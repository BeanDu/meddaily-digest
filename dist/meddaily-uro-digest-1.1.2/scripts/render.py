#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""医学文献日报 · 摘要编排与渲染层（含数字保真校验）

两个子命令（配合宿主 agent 的 LLM 能力）：
  plan      读 data/papers.json → 排除近 7 天已推送（本地库）与无摘要/无 pmid 篇 →
            输出待摘要清单 outputs/pending-<date>.json（每篇：pmid/title/abstract/期刊/score）
  finalize  读 outputs/agent-summaries-<date>.json（agent 生成）→
            逐篇【数字保真校验】（数字必须出现在原文；IL-17/DAS28 类药名数字豁免）→
            渲染日报 outputs/<file_prefix>-<date>.md + 入本地库；
            不合规篇目列在终端与报告尾部（agent 应修正后重新 finalize）

科室参数（key/dept/file_prefix）来自包根 spec.json，各科包共用本脚本。
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
OUT = os.path.join(PKG, "outputs")
DATA = os.path.join(PKG, "data")
os.makedirs(OUT, exist_ok=True)
os.makedirs(DATA, exist_ok=True)
os.environ.setdefault("CLINICAL_DB", os.path.join(DATA, "digest.db"))
sys.path.insert(0, HERE)

PKG_SPEC = json.load(open(os.path.join(PKG, "spec.json"), encoding="utf-8"))
KEY = PKG_SPEC["key"]            # 中心服务科室 key（如 rheumatology）
DEPT = PKG_SPEC["dept"]          # 科室中文名（如 风湿科）
FILE_PREFIX = PKG_SPEC["file_prefix"]  # 日报文件名前缀（如 rheum-digest）

# 药名/量表名自带的数字不计入密度与保真校验（跨科通用：分子/药物 + 常用评估量表）
_DRUG_NUM = re.compile(
    r"(?:IL|JAK|TYK|CD|HLA|BTK|PD|COX|SLEDAI|DAS|ACR|EULAR|"
    r"SOFA|APACHE|NYHA|GOLD|mMRC|BODE|FEV1)[ -]?\d+[A-Za-z]*", re.I)
_NUM = re.compile(r"\d+(?:\.\d+)?")

# 送 agent 的摘要字符上限：防指南/共识类超长摘要的长尾（实测 p99≈3163）。
# finalize 的数字保真校验也以这份截断后文本为唯一来源——agent 读到与校验依据一致，不会误打回。
_ABSTRACT_CAP = 3000


def db_pushed_recently(date):
    """近 7 天已推送（本地库 summaries，按 ukey）集合——跨天去重。"""
    import sqlite3
    db = os.environ["CLINICAL_DB"]
    if not os.path.exists(db):
        return set()
    con = sqlite3.connect(db, timeout=15)
    try:
        rows = con.execute(
            "SELECT DISTINCT ukey FROM summaries WHERE summary_ok=1 AND date<? AND date>=date(?,'-7 day')",
            (date, date)).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()
    finally:
        con.close()


def cmd_plan(date, top):
    import db as DB
    papers = json.load(open(os.path.join(DATA, "papers.json"), encoding="utf-8"))
    # 免费轨抓取产物用顶层 date；兼容中心 feed 的 feed_meta.date
    feed_date = str(papers.get("date") or (papers.get("feed_meta") or {}).get("date") or "")
    if feed_date and feed_date != date:
        sys.exit("❌ 数据日期为 %s，与目标日期 %s 不符：请重新抓取，或以 --date %s 渲染该日数据"
                 % (feed_date, date, feed_date))
    pushed = db_pushed_recently(date)
    pool = papers.get("papers") or []
    fresh = [p for p in pool if DB.paper_ukey(p) not in pushed]
    # 无摘要（数字校验依赖原文）、无 pmid（速览按 pmid 匹配）、仅模板的空壳摘要不进待摘要清单
    with_abs = [p for p in fresh if (p.get("abstract") or "").strip() and p.get("pmid")]
    cand = [p for p in with_abs if not _abstract_stub(p.get("abstract"))]
    picked = cand[:top]
    payload = {
        "date": date, "top": top,
        "candidates": len(cand), "skipped_pushed_7d": len(pool) - len(fresh),
        "skipped_filtered": len(fresh) - len(cand),
        "skipped_stub": len(with_abs) - len(cand),
        "items": [{
            "pmid": p.get("pmid", ""), "title": p.get("title", ""),
            "journal": p.get("journal_iso") or p.get("journal", ""),
            "pub_date": p.get("pub_date", ""), "score": p.get("score", 0),
            "section": p.get("section", ""),
            "abstract": (p.get("abstract") or "")[:_ABSTRACT_CAP] or "（无摘要）",
        } for p in picked],
    }
    path = os.path.join(OUT, "pending-%s.json" % date)
    json.dump(payload, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("待摘要 %d 篇（候选 %d，近7天已推送剔除 %d，无摘要/无编号/空壳模板滤除 %d）→ %s"
          % (len(picked), len(cand), len(pool) - len(fresh), len(fresh) - len(cand), path))



_STUB_BOX = re.compile(r"\[\s*(?:box\s*:\s*)?see text\s*\]", re.I)


def _abstract_stub(a):
    """空壳摘要判定：仅含方法学/免责模板（如 ASCO living guideline 的 "Box: see text"），
    无 RESULTS./RECOMMENDATIONS. 等实质结构段——这类条目无法产出有价值速览，直接滤除。"""
    t = (a or "").strip()
    core = _STUB_BOX.sub("", t).strip()
    if len(core) < 120:  # 剥离标记后几乎无内容
        return True
    if _STUB_BOX.search(t) and not re.search(r"\b(?:RESULTS|RECOMMENDATIONS)\.", core):
        return True
    return False


def _nums_in(text):
    clean = _DRUG_NUM.sub("", text or "")
    return set(_NUM.findall(clean))


def cmd_finalize(date):
    pend = json.load(open(os.path.join(OUT, "pending-%s.json" % date), encoding="utf-8"))
    summ_path = os.path.join(OUT, "agent-summaries-%s.json" % date)
    summaries = {str(s["pmid"]): s for s in json.load(open(summ_path, encoding="utf-8")) if s.get("pmid")}
    bad, items = [], []
    for it in pend["items"]:
        s = summaries.get(str(it["pmid"]))
        if not s:
            bad.append((it, "缺摘要"))
            continue
        body = s.get("summary")
        if not isinstance(body, str) or not body.strip():
            bad.append((it, "摘要为空或格式异常（summary 须为非空字符串）"))
            continue
        src_nums = _nums_in(it["abstract"] + " " + it["title"])
        ghost = [n for n in _nums_in(body) if n not in src_nums]
        if ghost:
            bad.append((it, "数字保真失败（原文没有）：" + ",".join(ghost[:5])))
            continue
        items.append((it, body))
    # 渲染
    L = ["# 医学文献日报-%s · %s" % (DEPT, date), ""]
    L.append("> 候选 %d 篇 · 精读 %d 篇 ｜ 数据源 Europe PMC / PubMed / ClinicalTrials.gov ｜ 摘要经数字保真校验"
             % (pend["candidates"], len(items)))
    L.append("")
    for sec in ["今日必读", "研究进展", "会议与指南", "快讯"]:
        grp = [(i, b) for i, b in items if i["section"] == sec]
        if not grp:
            continue
        L.append("## %s（%d 篇）" % (sec, len(grp)))
        L.append("")
        for it, body in grp:
            L.append("### %s ｜ %s（%s，score %.0f）"
                     % (it["title"][:160], it["journal"][:36], it["pub_date"], it["score"]))
            for ln in body.strip().splitlines():
                if ln.strip():
                    L.append("- " + ln.strip())
            if it["pmid"]:
                L.append("- 原文：https://pubmed.ncbi.nlm.nih.gov/%s/" % it["pmid"])
            L.append("")
    if bad:
        L.append("## ⚠ 数字保真未通过（%d 篇，需修正后重新 finalize）" % len(bad))
        for it, why in bad:
            L.append("- %s：%s" % (it["title"][:60], why))
    report = os.path.join(OUT, "%s-%s.md" % (FILE_PREFIX, date))
    open(report, "w", encoding="utf-8").write("\n".join(L))
    # 入库
    import db as DB
    conn = DB.connect()
    rid = DB.begin_run(conn, KEY, date, "digest")
    papers = json.load(open(os.path.join(DATA, "papers.json"), encoding="utf-8"))
    DB.ingest_papers(conn, KEY, date, papers, rid)
    # 入库前按 ukey 去重（pmid→doi→title 前 60 字），全量入库：
    # 仅"本次待摘要清单内、且数字保真通过"的篇目 summary_ok=1（参与"已推送"去重）；
    # 清单外（top 截断/滤除）、未过校验、缺摘要的篇目 summary_ok=0，次日重新进入待摘要清单可重试。
    pend_pmids = {str(it["pmid"]) for it in pend["items"]}
    bad_pmids = {str(it["pmid"]) for it, _ in bad}
    seen, uniq = set(), []
    for p in papers.get("papers") or []:
        k = p.get("pmid") or p.get("doi") or (p.get("title") or "")[:60]
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(p)

    def _ok(pmid):
        pmid = str(pmid or "")
        return pmid and pmid in pend_pmids and pmid in summaries and pmid not in bad_pmids

    sp = {"papers": [dict(p, summary=(summaries.get(str(p.get("pmid", ""))).get("summary", "")
                                      if _ok(p.get("pmid", "")) else ""),
                          summary_ok=1 if _ok(p.get("pmid", "")) else 0)
                     for p in uniq]}
    DB.ingest_summaries(conn, KEY, date, sp, rid)
    DB.end_run(conn, rid, "ok", counts={"reports": len(items), "bad": len(bad)})
    print("✅ 日报：%s（精读 %d，未过校验 %d）" % (report, len(items), len(bad)))
    for it, why in bad:
        print("  ❌ %s ← %s" % (it["title"][:50], why))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "finalize"])
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()
    if a.cmd == "plan":
        cmd_plan(a.date, a.top)
    else:
        sys.exit(cmd_finalize(a.date))


if __name__ == "__main__":
    main()
