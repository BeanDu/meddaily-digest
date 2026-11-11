#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
医学文献日报 — 数据抓取层（多科室通用）

多源采集：
  1. Europe PMC  主源，按 FIRST_PDATE（发表日，无 MeSH 索引延迟）
  2. PubMed      补 MeSH / PublicationType / 精确发表日期
  3. ClinicalTrials.gov v2  在研管线（Phase 2/3）
  4. PubMed      会议摘要（学会增刊 + 会议标记）

科室特异的检索词 / 期刊权重 / 会议识别 / CT.gov 条件全部来自
specs/<name>.json，用 --spec 指定。
默认科室取包内 spec.DEFAULT_SPEC，可用 --spec 或 CLINICAL_SPEC 覆盖。

输出: data/papers.json  已评分、已分栏、已去重
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ── 科室配置 ──────────────────────────────────────────────────────
# 一份代码跑多个科室：检索词、期刊权重、会议识别、CT.gov 条件都在 spec 里。
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)  # 同目录的 spec 模块
from spec import load as _load_spec, add_spec_arg   # noqa: E402

def _apply_spec(sp):
    """切换/初始化科室：刷新模块级 spec 派生表（含热点词表）。"""
    global SPEC, DISEASES, UA, TOPIC_WORDS
    SPEC = sp
    DISEASES = SPEC["diseases"]
    UA = SPEC["ua"]
    TOPIC_WORDS = [(p, int(w)) for p, w in SPEC.get("topic_words", [])]


_apply_spec(_load_spec())

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CTGOV = "https://clinicaltrials.gov/api/v2/studies"

# 代理支持：部分网络环境需经本地代理
# 可通过环境变量 https_proxy / HTTPS_PROXY / FETCH_PROXY 注入
_PROXY = (os.environ.get("FETCH_PROXY")
          or os.environ.get("https_proxy")
          or os.environ.get("HTTPS_PROXY", ""))
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY}) if _PROXY else urllib.request.ProxyHandler({})
)
if _PROXY:
    print(f"[网络] 使用代理: {_PROXY}")

# 产物目录：默认包内 data/，可用 CLINICAL_OUTPUT_DIR 覆盖
OUTPUT_DIR = os.environ.get("CLINICAL_OUTPUT_DIR") or os.path.join(os.path.dirname(_HERE), "data")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── 节流（NCBI 无 key 限 3 req/s，EPMC 宽松但也留余量）────────────────
_LAST = {"t": 0.0}
MIN_GAP = {"epmc": 0.35, "ncbi": 0.42, "ctgov": 0.4}


def _throttle(kind):
    gap = MIN_GAP[kind] - (time.time() - _LAST["t"])
    if gap > 0:
        time.sleep(gap)
    _LAST["t"] = time.time()


def http_get(url, kind="epmc", tries=4, timeout=35):
    """带节流与重试的 GET。返回 bytes；源失败（网络/限流/网关）返回 None——与"查无结果"区分。"""
    last_exc = None
    for attempt in range(tries):
        _throttle(kind)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with _OPENER.open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code in (429, 503) and attempt < tries - 1:
                # 限流：按 Retry-After 或指数退避（限流绝不能当"查无结果"处理）
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = int(ra) if ra and str(ra).isdigit() else 2 ** (attempt + 1)
                wait = min(wait, 30)
                print(f"  … 限流 {e.code}，{wait}s 后重试（{attempt + 1}/{tries}）", file=sys.stderr)
                time.sleep(wait)
                continue
        except Exception as e:
            last_exc = e
        if attempt < tries - 1:
            print(f"  … 请求失败（{type(last_exc).__name__}），第 {attempt + 1}/{tries} 次退避重试",
                  file=sys.stderr)
            time.sleep(1.5 * (attempt + 1))
        else:
            print(f"  ! 请求失败 {url[:110]} -> {type(last_exc).__name__}: {last_exc}", file=sys.stderr)
    return None


# ══════════════════════════════════════════════════════════════════════
# 病种/期刊权重/热点词等科室知识全部来自 specs/<name>.json
# （由 _apply_spec 初始化，--spec 切换时统一刷新）

# ── 出版类型权重 ────────────────────────────────────────────────────
PT_SCORE = {
    "Randomized Controlled Trial": 32, "Meta-Analysis": 28, "Systematic Review": 26,
    "Practice Guideline": 30, "Guideline": 28, "Clinical Trial": 20,
    "Multicenter Study": 16, "Comparative Study": 10, "Observational Study": 8,
    "Review": 12, "Systematic Review; Meta-Analysis": 28,
}
PT_PENALTY = {
    "Case Reports": -12, "Editorial": -26, "Comment": -28, "Letter": -30,
    "News": -30, "Retracted Publication": -80, "Published Erratum": -35,
    "Preprint": -8,
}


MECHANISM_WORDS = re.compile(
    r"\b(mechanis|pathogenesis|pathophysiolog|microRNA|miRNA|lncRNA|single[- ]cell|"
    r"transcriptom|proteom|metabolom|epigenet|immunohistochem|cytokine|autoantibod|"
    r"molecular dock|structure[- ]based|ferroptos|pyroptos|exosome)\b", re.I)


# ── 期刊白名单查表（v1.0.0 纯白名单评分）──
def _jw_norm(s):
    if not s:
        return ''
    s = str(s).replace('\u00a0', ' ').strip()
    s = re.sub(r'[.\u00a0]+$', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.upper()


def _jw_base(s):
    """宽松基名：去 ': 副标题' / ' = 另一刊名' / 尾部 '(城市, ST)' / 前导 'The'。"""
    s = _jw_norm(s)
    if not s:
        return ''
    s = re.sub(r'\s*[:=].*$', '', s)
    s = re.sub(r'\s*\([^)]*\)\s*$', '', s)
    s = re.sub(r'^THE\s+', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def _jw_tables(spec):
    j = (spec or {}).get('journals') or {}
    return j.get('top') or {}, j.get('core') or {}, j.get('sci') or {}


def _manual_weight(journal, journal_iso, spec):
    """手工白名单查表。返回 (score, tier, matched_key)；未命中 (0, None, None)。

    先按 journal_iso / journal 精确匹配，再按宽松基名匹配。
    """
    top, core, sci = _jw_tables(spec)
    tables = (('top', top), ('core', core), ('sci', sci))
    for cand in (journal_iso, journal):
        k = _jw_norm(cand)
        if not k:
            continue
        for tier, t in tables:
            if k in t:
                return t[k], tier, k
    for cand in (journal_iso, journal):
        b = _jw_base(cand)
        if not b:
            continue
        best = None
        for tier, t in tables:
            for k2, v in t.items():
                if _jw_base(k2) == b and (best is None or v > best[0]):
                    best = (v, tier, k2)
        if best:
            return best
    return 0, None, None


def _journal_weight_impl(journal, journal_iso=None, spec=None):
    """期刊权重（纯手工白名单）。返回 (score, reason)；未命中 → (0, '')。

    必须同时传 journal_iso —— 白名单 key 是 NLM 缩写（Ann Rheum Dis），
    而 EPMC 返回的是全称（Annals of the rheumatic diseases）；
    只用全称查表会让绝大多数命中失败（历史 bug：候选池里 92% 的期刊分得 0）。
    """
    sc, tier, key = _manual_weight(journal, journal_iso, spec)
    if sc:
        return sc, '期刊+%d(%s)' % (sc, key)
    return 0, ''



def topic_score(text: str) -> int:
    s = 0
    for pat, w in TOPIC_WORDS:
        if re.search(pat, text, re.I):
            s += w
    return min(s, 34)


def detect_diseases(title: str, abstract: str, mesh=None):
    """病种识别（分层信号，避免误标）

    优先级：
      1. MeSH 主题词命中（最可靠，但有 1-3 周索引延迟）
      2. 疾病全名出现在 标题+摘要（大小写不敏感）
      3. 缩写出现在 **标题**（大小写敏感，避免摘要里 "as"/"ra"/"sle" 等英文常用词误命中）
    """
    hits = []
    title = title or ""
    blob = f"{title} {abstract or ''}"
    mesh_l = " | ".join(mesh or []).lower()
    for d in DISEASES:
        ok = False
        mk = d.get("mesh_key")
        if mk and mk.lower() in mesh_l:
            ok = True
        if not ok:
            for pat in d["match"]:
                if re.search(pat, blob, re.I):
                    ok = True
                    break
        if not ok:
            for pat in d.get("abbr", []):
                if re.search(pat, title):   # 仅标题，大小写敏感
                    ok = True
                    break
        if ok:
            hits.append(d["key"])
    return hits


MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def days_between(d: str, ref: datetime) -> int:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return 99
    return max(0, (ref - dt).days)


# ══════════════════════════════════════════════════════════════════════
# Europe PMC 检索
# ══════════════════════════════════════════════════════════════════════
def epmc_search(query: str, page_size=100):
    q = urllib.parse.urlencode({
        "query": query, "format": "json", "pageSize": page_size,
        "resultType": "core", "sort": "P_PDATE_D desc",
    })
    raw = http_get(f"{EPMC}/search?{q}", kind="epmc")
    if raw is None:
        return None  # 源失败（网络/限流），与"查无结果"[]区分
    try:
        return json.loads(raw).get("resultList", {}).get("result", [])
    except Exception:
        print("  ! EPMC 响应非 JSON（可能被网关改写），按源失败处理", file=sys.stderr)
        return None


def _epmc_record(r):
    """Europe PMC 结果 → 统一文献记录（src / is_conference 由调用方补充）。"""
    jinfo = r.get("journalInfo", {})
    jiss = jinfo.get("journalIssue", {})
    return {
        "pmid": r.get("pmid") or "",
        "pmcid": r.get("pmcid") or "",
        "title": re.sub(r"<[^>]+>", "", r.get("title", "") or "").strip(),
        "journal": jinfo.get("journal", {}).get("title", "") or "",
        "journal_iso": jinfo.get("journal", {}).get("isoabbreviation", "") or "",
        "volume": jiss.get("volume", "") or "",
        "issue": jiss.get("issue", "") or "",
        "pub_date": r.get("firstPublicationDate", "") or "",
        "pub_types": r.get("pubTypeList", {}).get("pubType", []) or [],
        "mesh": [m.get("descriptorName", "") for m in r.get("meshHeadingList", {}).get("meshHeading", [])],
        "abstract": re.sub(r"<[^>]+>", "", r.get("abstractText", "") or "").strip(),
        "doi": r.get("doi", "") or "",
        "authors": [a.get("fullName", "") for a in r.get("authorList", {}).get("author", [])][:6],
        "is_oa": r.get("isOpenAccess") == "Y",
        "in_epmc": r.get("inEPMC") == "Y",
        "cited_by": r.get("citedByCount", 0) or 0,
    }


def fetch_literature(ref_date: datetime, window_days=3):
    """主源：Europe PMC，按 FIRST_PDATE 窗口"""
    start = (ref_date - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = ref_date.strftime("%Y-%m-%d")
    print(f"[EPMC] 窗口 {start} → {end}")

    queries = []
    for d in DISEASES:
        terms = " OR ".join(f"TITLE_ABS:{t}" for t in d["epmc"])
        queries.append((d["key"], f"({terms}) AND (FIRST_PDATE:[{start} TO {end}])"))

    seen, out = set(), []
    failed = 0
    for key, q in queries:
        rows = epmc_search(q)
        if rows is None:
            failed += 1
            print(f"  {key}: ⚠ 源失败（网络/限流，与零命中区分记录）")
            continue
        print(f"  {key}: {len(rows)} 条")
        for r in rows:
            pmid = r.get("pmid") or ""
            key_id = pmid or r.get("doi") or r.get("title", "")[:60]
            if not key_id or key_id in seen:
                continue
            seen.add(key_id)
            rec = _epmc_record(r)
            rec["src"] = "epmc"
            # 会议摘要识别：科室增刊（各学会年会增刊）或其他 congress/meeting abstract
            blob = f"{rec['title']} {rec['abstract']}".lower()
            _conf = SPEC["conference"]
            _is_conf_journal = any(
                mk in (rec.get("journal") or "").lower() for mk in _conf["journal_markers"])
            has_conf = re.search(
                r"\b(congress|meeting abstract|conference abstract|annual meeting|"
                + _conf["blob_terms"] + r")\b", blob)
            rec["is_conference"] = bool(
                (_is_conf_journal and (has_conf or "suppl" in str(rec.get("issue", "")).lower()))
                or re.search(r"\b(congress|meeting abstract|conference abstract|annual meeting)\b", blob)
            )
            out.append(rec)
    if failed:
        print(f"  ⚠ {failed}/{len(queries)} 个病种查询源失败（网络/限流）——本次结果可能偏薄")
    return out


def fetch_conference_epmc(ref_date: datetime, window_days=120):
    """会议摘要（主路径）：Europe PMC + 标题会议关键词 + 病种约束

    依据实测：PubMed ``Meeting Abstract[pt]`` 近年基本零命中（不再索引会议摘要），
    学会增刊在 efetch 里 issue 常为空；EPMC 按标题会议词可召回真实会议出版物。
    ⚠️ 必须加病种约束：仅用会议关键词会抓到大量无关学科的会议出版物。
    会议增刊发表有滞后，窗口取 120 天。
    """
    start = (ref_date - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = ref_date.strftime("%Y-%m-%d")
    print(f"[会议摘要] Europe PMC 标题会议关键词 · 窗口 {start} → {end}")

    _titles = " OR ".join(f'TITLE:"{t}"' for t in SPEC["conference"]["epmc_titles"])
    q = (f'({_titles}) '
         f'AND (FIRST_PDATE:[{start} TO {end}]) '
         f'AND ({" OR ".join(f"TITLE_ABS:{t}" for d in DISEASES for t in d["epmc"])})')
    rows = epmc_search(q)
    if rows is None:
        print("  ⚠ 会议源失败（网络/限流，跳过本次会议摘要）")
        return []
    print(f"  命中 {len(rows)} 条")

    out = []
    for r in rows:
        rec = _epmc_record(r)
        rec["src"] = "epmc_conf"
        rec["is_conference"] = True
        out.append(rec)
    return out


def fetch_conference(ref_date: datetime, window_days=45):
    """会议摘要（辅路径）：PubMed 检索学会期刊增刊（spec: conference.pubmed_jour）"""
    _conf = SPEC["conference"]
    print("[会议摘要] PubMed 检索 %s + 会议标记" % _conf["pubmed_jour"])
    start = (ref_date - timedelta(days=window_days)).strftime("%Y/%m/%d")
    end = ref_date.strftime("%Y/%m/%d")
    term = ('%s AND (%s) AND ("%s"[dp] : "%s"[dp])'
            % (_conf["pubmed_jour"], _conf["pubmed_terms"], start, end))
    q = urllib.parse.urlencode({"db": "pubmed", "term": term, "retmax": 120, "retmode": "json", "sort": "pub_date"})
    raw = http_get(f"{EUTILS}/esearch.fcgi?{q}", kind="ncbi")
    if not raw:
        print("  ⚠ PubMed 会议源不可用（网络/限流，跳过）")
        return []
    try:
        ids = json.loads(raw)["esearchresult"].get("idlist", [])
    except Exception:
        return []
    print(f"  命中 {len(ids)} 条")
    if not ids:
        return []
    recs = fetch_pubmed_xml(ids)
    # ⚠️ 不能无条件标 is_conference=True！
    # 实测：PubMed 的 Meeting Abstract[pt] 近 90 天命中 0，Ann Rheum Dis 增刊在 efetch 里
    # issue 为空、PublicationType 是普通 Journal Article —— 强制标记会把
    # Correspondence/Review 误标成会议摘要。改用增刊 issue / 会议关键词严格判定。
    for r in recs:
        issue = str(r.get("issue", "") or "").lower()
        blob = f"{r.get('title','')} {r.get('abstract','')}".lower()
        r["is_conference"] = bool(
            "suppl" in issue
            or "meeting abstract" in (r.get("pub_types") or [])
            or re.search(r"\b(congress|annual meeting|conference abstract|meeting abstract)\b", blob)
        )
    return recs


def fetch_pubmed_xml(pmids):
    """PubMed efetch：拿 MeSH + PublicationType + 精确日期"""
    q = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
    raw = http_get(f"{EUTILS}/efetch.fcgi?{q}", kind="ncbi")
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    out = []
    for art in root.findall(".//PubmedArticle"):
        med = art.find("MedlineCitation")
        art_el = med.find("Article") if med is not None else None
        if art_el is None:
            continue
        pmid = med.findtext("PMID", "")
        title_el = art_el.find("ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        journal = art_el.findtext(".//Journal/Title", "") or ""
        iso = art_el.findtext(".//Journal/ISOAbbreviation", "") or ""
        pd = art_el.find(".//JournalIssue/PubDate")
        pub_date = ""
        if pd is not None:
            y = pd.findtext("Year", "")
            m = pd.findtext("Month", "")
            d = pd.findtext("Day", "")
            m_num = MONTH_NUM.get((m or "").strip().lower())
            if y and m_num and d and d.isdigit():
                pub_date = f"{y}-{m_num:02d}-{int(d):02d}"
            elif y and m_num:
                pub_date = f"{y}-{m_num:02d}"
            elif y and m and m.isdigit():
                pub_date = f"{y}-{int(m):02d}"
            elif y and m:
                # MedlineDate 形式: "2026 Oct 20" / "2026 Oct-Dec"
                md = pd.findtext("MedlineDate", "")
                mt = re.search(r"(\d{4})\s+([A-Za-z]{3})", md or "")
                if mt:
                    mm = MONTH_NUM.get(mt.group(2).lower())
                    if mm:
                        pub_date = f"{mt.group(1)}-{mm:02d}"
                elif y.isdigit():
                    pub_date = y
            elif y and y.isdigit():
                pub_date = y
        absts = []
        for a in art_el.findall(".//Abstract/AbstractText"):
            lbl = a.get("Label")
            txt = "".join(a.itertext()).strip()
            absts.append(f"{lbl}. {txt}" if lbl else txt)
        mesh = [m.findtext("DescriptorName", "") for m in art.findall(".//MeshHeadingList/MeshHeading")]
        doi = ""
        for i in art.findall(".//ArticleId"):
            if i.get("IdType") == "doi":
                doi = i.text or ""
        out.append({
            "pmid": pmid,
            "title": title,
            "journal": journal,
            "journal_iso": iso,
            "pub_date": pub_date,
            "pub_types": [t.text for t in art.findall(".//PublicationType") if t.text],
            "mesh": mesh,
            "abstract": "\n".join(absts),
            "doi": doi,
            "authors": [a.findtext("LastName", "") for a in art.findall(".//AuthorList/Author")][:6],
            "is_conference": bool(re.search(r"\b(congress|meeting abstract|conference abstract)\b",
                                            (title + " ".join(absts)).lower())),
            "src": "pubmed",
        })
    return out


def fetch_trials(ref_date: datetime, window_days=45):
    """ClinicalTrials.gov v2：在研管线（Phase 2/3）"""
    print("[CT.gov] 在研管线检索")
    conds = list(SPEC["ctgov_conditions"])
    start = (ref_date - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = ref_date.strftime("%Y-%m-%d")
    out, seen = [], set()
    for cond in conds:
        # CT.gov filter.advanced 语法：括号组 + 日期 RANGE，括号必须配对
        filt = ("(AREA[Phase]PHASE2 OR AREA[Phase]PHASE3) "
                f"AND AREA[LastUpdatePostDate]RANGE[{start},{end}]")
        q = urllib.parse.urlencode({
            "query.cond": cond, "filter.advanced": filt, "format": "json",
            "pageSize": 25, "countTotal": "true",
        })
        raw = http_get(f"{CTGOV}?{q}", kind="ctgov")
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        for s in d.get("studies", []):
            p = s.get("protocolSection", {})
            idm = p.get("identificationModule", {})
            st = p.get("statusModule", {})
            des = p.get("designModule", {})
            nct = idm.get("nctId", "")
            if not nct or nct in seen:
                continue
            seen.add(nct)
            phases = des.get("phases", []) or []
            out.append({
                "nct": nct,
                "title": idm.get("briefTitle", "") or "",
                "official_title": idm.get("officialTitle", "") or "",
                "sponsor": (p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}) or {}).get("name", ""),
                "phase": "/".join(phases),
                "status": st.get("overallStatus", "") or "",
                "enrollment": des.get("enrollmentInfo", {}).get("count", ""),
                "last_update": st.get("lastUpdatePostDateStruct", {}).get("date", "") or "",
                "condition": cond,
                "url": f"https://clinicaltrials.gov/study/{nct}",
            })
        time.sleep(0.2)
    print(f"  命中 {len(out)} 项")
    return out


# ══════════════════════════════════════════════════════════════════════
# 评分与分栏
# ══════════════════════════════════════════════════════════════════════
def score_paper(p, ref_date: datetime):
    reasons = []
    s = 0.0
    jw, jw_reason = _journal_weight_impl(p.get("journal", ""), p.get("journal_iso", ""), SPEC)
    if jw:
        s += jw
        reasons.append(jw_reason)

    pt = " ".join(p.get("pub_types", []) or [])
    pt_best = 0
    for k, v in PT_SCORE.items():
        if k.lower() in pt.lower() and v > pt_best:
            pt_best = v
    if pt_best:
        s += pt_best
        reasons.append(f"类型+{pt_best}")
    pen = 0
    for k, v in PT_PENALTY.items():
        if k.lower() in pt.lower():
            pen = min(pen, v)
    if pen:
        s += pen
        reasons.append(f"类型{pen}")

    blob = f"{p.get('title','')} {p.get('abstract','')}"
    tw = topic_score(blob)
    if tw:
        s += tw
        reasons.append(f"热点+{tw}")

    if p.get("mesh"):
        s += 10
        reasons.append("MeSH已标引+10")
    if p.get("is_oa") or p.get("in_epmc"):
        s += 8
        reasons.append("OA全文+8")
    if len(p.get("abstract", "")) > 600:
        s += 5
        reasons.append("摘要详实+5")

    age = days_between(p.get("pub_date", "")[:10], ref_date)
    if age <= 1:
        s += 15; reasons.append("最新+15")
    elif age <= 3:
        s += 10; reasons.append("近3天+10")
    elif age <= 7:
        s += 5; reasons.append("近7天+5")

    if p.get("is_conference"):
        # ⚠️ 会议/增刊内容天然滞后（会议源窗口为 120 天），不能套用「较旧-20」
        # 双重扣分会把真会议文献挤出精读栏，与专门抓会议摘要的目标冲突
        if age > 60:
            s -= 8; reasons.append("会议发布滞后-8")
        s += 8; reasons.append("会议/综述+8")
    elif age > 60:
        s -= 20; reasons.append("较旧-20")
    if MECHANISM_WORDS.search(blob):
        s += 6
        reasons.append("机制研究+6")

    diseases = detect_diseases(p.get("title", ""), p.get("abstract", ""), p.get("mesh"))
    p["diseases"] = diseases
    p["disease_zh"] = [d["zh"] for d in DISEASES if d["key"] in diseases]
    if diseases:
        s += 6
        reasons.append("病种匹配+6")

    p["score"] = round(s, 1)
    p["score_reasons"] = reasons
    return p


def assign_section(p):
    pt = " ".join(p.get("pub_types", []) or []).lower()
    blob = f"{p.get('title','')} {p.get('abstract','')}".lower()
    if p.get("is_conference"):
        return "会议与指南"
    if "practice guideline" in pt or "guideline" in pt or re.search(
            r"\b(consensus|recommendations?|task force|guidelines for)\b", blob):
        return "会议与指南"
    if re.search(r"\b(randomized controlled trial|meta-analysis|systematic review|practice guideline)\b", pt) \
            or (re.search(r"\b(network meta-analysis)\b", blob)):
        return "今日必读"
    if MECHANISM_WORDS.search(blob) and not re.search(r"\b(patients|cohort|trial|participants)\b", blob):
        return "研究进展"
    if p.get("score", 0) >= 45:
        return "今日必读"
    return "快讯"


# ══════════════════════════════════════════════════════════════════════
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--window", type=int, default=3, help="文献窗口天数")
    add_spec_arg(ap)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # 换科室：统一走 _apply_spec 刷新全部派生表
    if args.spec:
        _apply_spec(_load_spec(args.spec))
    if not args.out:
        args.out = os.path.join(OUTPUT_DIR, "papers.json")

    ref = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.date \
        else datetime.now(timezone.utc)
    print(f"=== {SPEC['zh']}资讯抓取 · 基准日 {ref.strftime('%Y-%m-%d')} · 窗口 {args.window} 天 ===")

    # ── 窗口阶梯：抓取量偏薄时逐级放宽窗口重试 ──────────────────
    # 上游 EPMC 对近期 FIRST_PDATE 的收录会滞后/回退（2026-11-06 实测）：
    #   3 天窗口 RA 只召回 2 条 → 总量 18 < 阈值 20 → 误判「网络故障」整天白跑；
    #   同一查询放宽到 10 天窗口召回 164 条。
    #   呼吸科/ICU 同日也整体塌陷（COPD 34→3、PNEU 40→11），只是靠会议摘要与
    #   在研管线凑过了阈值，没报错但内容明显变薄 —— 所以判据不能只看「是否报错」。
    # 「偏薄」的基线取上一次成功抓取的量：不足 40% 就放宽窗口。
    # 放宽窗口不会引入陈旧内容：下游 render.py 的跨天去重会剔除近 7 天已精读推送过的。
    MIN_VALID = 20
    WINDOW_LADDER = [3, 6, 10]
    HEALTHY_RATIO = 0.4

    base = 0
    try:
        with open(args.out, encoding="utf-8") as fh:
            _c = json.load(fh).get("counts") or {}
            # 与本次比较口径对齐（本次用"仅文献数"）：total 含会议合并，需扣除"会议指南"栏
            base = max(0, int(_c.get("total") or 0) - int(_c.get("会议指南") or 0))
    except Exception:
        base = 0
    floor = max(MIN_VALID, int(base * HEALTHY_RATIO)) if base else MIN_VALID

    papers = []
    used_window = args.window
    for _w in [args.window] + [x for x in WINDOW_LADDER if x > args.window]:
        papers = fetch_literature(ref, _w)
        used_window = _w
        if len(papers) >= floor:
            break
        _nxt = [x for x in WINDOW_LADDER if x > _w]
        if _nxt:
            print(f"   ⚠ EPMC 文献 {len(papers)} 篇 < 健康下限 {floor}（上次成功 {base}）"
                  f" → 放宽到 {_nxt[0]} 天窗口重试")
        else:
            print(f"   ⚠ EPMC 文献 {len(papers)} 篇 < 健康下限 {floor}（上次成功 {base}），"
                  f"且已到最大窗口 {_w} 天")
    print(f"[窗口] 最终使用 {used_window} 天（EPMC 文献 {len(papers)} 篇，健康下限 {floor}）")
    args.window = used_window

    # PubMed 补 MeSH / PublicationType（对 EPMC 命中项）
    pmids = [p["pmid"] for p in papers if p.get("pmid")]
    if pmids:
        print(f"[PubMed] 补全 {len(pmids)} 条 MeSH/类型...")
        for i in range(0, len(pmids), 100):
            batch = pmids[i:i + 100]
            for rec in fetch_pubmed_xml(batch):
                tgt = next((p for p in papers if p.get("pmid") == rec["pmid"]), None)
                if not tgt:
                    continue
                tgt["mesh"] = tgt.get("mesh") or rec["mesh"]
                tgt["pub_types"] = tgt.get("pub_types") or rec["pub_types"]
                if len(rec["pub_date"]) > len(tgt.get("pub_date", "")):
                    tgt["pub_date"] = rec["pub_date"]
                if len(rec["abstract"]) > len(tgt.get("abstract", "")):
                    tgt["abstract"] = rec["abstract"]
                tgt["doi"] = tgt.get("doi") or rec["doi"]

    # 会议摘要：EPMC 标题会议关键词（主）+ PubMed Ann Rheum Dis 增刊（辅）
    conf = fetch_conference_epmc(ref) + fetch_conference(ref)
    have = {p.get("pmid") for p in papers if p.get("pmid")}
    for c in conf:
        if c.get("pmid") and c["pmid"] in have:
            tgt = next(p for p in papers if p.get("pmid") == c["pmid"])
            tgt["is_conference"] = True
            if not tgt.get("mesh"):  # _epmc_record 恒写 mesh 键（可为空表），仅在空时用会议记录补
                tgt["mesh"] = c.get("mesh") or []
            continue
        c["is_oa"] = c.get("is_oa", False)
        c["in_epmc"] = c.get("in_epmc", False)
        c["cited_by"] = c.get("cited_by", 0)
        c["pmcid"] = c.get("pmcid", "")
        papers.append(c)

    # 去重（同 DOI / 同标题）
    seen, dedup = set(), []
    for p in papers:
        k = (p.get("doi") or "").lower() or re.sub(r"\W+", "", p.get("title", "").lower())[:70]
        if not k or k in seen:
            continue
        seen.add(k)
        dedup.append(p)
    papers = dedup

    # ⚠️ 失败保护：抓取量低于阈值时，绝不覆盖已有好数据
    # （窗口阶梯已在上面放宽过；到这里仍不足 → 真的是网络/接口故障）
    if len(papers) < MIN_VALID and os.path.exists(args.out):
        print(f"\n❌ 本次仅抓取到 {len(papers)} 篇（低于阈值 {MIN_VALID}），判定为网络故障。")
        print(f"   保留已有数据不变：{args.out}")
        print("   请检查网络/代理后重试（可设 https_proxy 或 FETCH_PROXY 环境变量）")
        sys.exit(2)
    if len(papers) < MIN_VALID:
        print(f"\n❌ 仅抓取到 {len(papers)} 篇，低于阈值 {MIN_VALID}，无法生成日报。")
        sys.exit(2)

    # 评分 + 分栏
    for p in papers:
        score_paper(p, ref)
        p["section"] = assign_section(p)
    papers.sort(key=lambda x: x.get("score", 0), reverse=True)

    trials = fetch_trials(ref)

    payload = {
        # 产物日期用本地时区（与 render 默认 --date 口径一致；显式 --date 时原样保留）
        "date": args.date or datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": args.window,
        "counts": {
            "total": len(papers),
            "必读": sum(1 for p in papers if p["section"] == "今日必读"),
            "进展": sum(1 for p in papers if p["section"] == "研究进展"),
            "会议指南": sum(1 for p in papers if p["section"] == "会议与指南"),
            "快讯": sum(1 for p in papers if p["section"] == "快讯"),
            "trials": len(trials),
        },
        "papers": papers,
        "trials": trials,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 抓取完成 → {args.out}")
    print(f"   文献 {len(papers)} 条 | 试验 {len(trials)} 项 | 分栏 {payload['counts']}")
    for p in papers[:8]:
        print(f"   [{p['score']:.0f}] {p['section']:<6} {(p.get('journal_iso') or p.get('journal',''))[:22]:<22} {p['title'][:62]}")


if __name__ == "__main__":
    main()
