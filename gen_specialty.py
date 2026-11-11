#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从免费通用源（digest/）生成专科包（27 科全量）。

单一通用源 digest/：2 份占位符模板（SKILL/README，市场文案与版本记录并入 README）
+ 4 份各科共用的通用脚本（fetch/render/spec/db，运行时读包内 spec.json）
+ 母版配色 icon.svg。digest/ 不含任何科室实例。

科室事实来自 specs/{key}.json（server/app/scripts/specs 的 7 字段投影）。
包级差异：
  SLUGS            包名缩写（slug = {SLUGS[key]}-digest）
  QUOTES           市场引用样例（仅真实产出过的科；其余科 README 不渲染示例块）
  KEYWORDS_OVERRIDE 定制搜索关键词（其余科自动拼接：科室名 + 前 6 病种中文）
  FIXED_PALETTES   品牌固定配色（已上架/已验证科）；其余科按色相自动分配

生成流程：占位符替换（逐文件断言命中 + 无残留）→ spec 放置与 spec.json
（key/dept/file_prefix）→ 图标换色 + png（失败即报错）→ py_compile →
权限归一（755/644）→ 打 zip（顶层 {slug}-{version}/，含空目录）。

验收（幂等零差异）：重跑后生成目录与 dist/ 现有目录 `diff -rq` 应零差异。

用法：python3 gen_specialty.py [key ...]（默认 specs/ 下全部科室）
环境：GEN_VERSION（默认 1.1.1）/ GEN_DEST（默认 ./dist）/ GEN_SRC（默认 ./digest）
"""
import colorsys
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("GEN_SRC", os.path.join(HERE, "digest"))
SPEC_DIR = os.path.join(HERE, "specs")
DEST_BASE = os.environ.get("GEN_DEST", os.path.join(HERE, "dist"))
VERSION = os.environ.get("GEN_VERSION", "1.1.1")  # 发版时同步在 digest/README.md 版本记录写条目（生成器会断言）

# 包名缩写（slug = meddaily-{SLUGS[key]}-digest）
SLUGS = {
    "rheumatology": "rheum", "respirology": "resp", "icu": "icu", "oncology": "onco",
    "cardiology": "cardio", "endocrinology": "endo", "gastroenterology": "gastro",
    "hematology": "heme", "infectious": "infect", "nephrology": "nephro",
    "neurology": "neuro", "anesthesiology": "anes", "cardiothoracic": "cts",
    "dentistry": "dental", "dermatology": "derm", "emergency": "em",
    "generalsurgery": "gs", "geriatrics": "geri", "neurosurgery": "nsurg",
    "obgyn": "obgyn", "ophthalmology": "ophth", "orthopedics": "ortho",
    "otolaryngology": "ent", "pediatrics": "peds", "psychiatry": "psych",
    "rehabilitation": "rehab", "urology": "uro",
}

# 市场引用样例（仅真实产出过的科；无 quote 的科 README 不渲染示例块）
QUOTES = {
    "rheumatology": ["痛风石患者按血尿酸 < 0.36 mmol/L 达标滴定时，达标率 70.3% vs 73.6%——质疑「有痛风石就得加量」的现行推荐。（J Rheumatol）。",
                     "SLE 治疗升级后黑人患者主要临床应答仅 10%，校正后比值比 0.13。（Ann Rheum Dis）。"],
    "respirology": ["ARDS 后肺纤维化——从分子机制到临床管理（Expert Rev Mol Med）。",
                    "ATS 官方声明：哮喘的按需吸入激素治疗（Am J Respir Crit Care Med）。"],
    "icu": ["感染性休克中新斯的明的双重保护机制探讨（Crit Care Med）。",
            "「概率不等于获益证据」——重症研究统计方法学批判性评价（Crit Care Med）。"],
    "oncology": ["PD-1 抑制剂联合化疗一线治疗非小细胞肺癌 5 年生存率 23.8% vs 16.2%（KEYNOTE-407 长期随访）。（J Clin Oncol）。",
                 "ctDNA 早期清除预测 III 期结肠癌辅助化疗获益，清除组 3 年无病生存提高 12%。（Ann Oncol）。"],
}

# 搜索关键词覆盖（其余科自动拼接）
KEYWORDS_OVERRIDE = {
    "rheumatology": "医学文献 风湿 风湿科 文献日报 医学速递 循证医学 PubMed 病例研讨 内科 文献管理",
    "respirology": "呼吸 呼吸科 文献日报 COPD 哮喘 肺癌 肺炎 间质性肺病 医学速递 循证医学 PubMed 病例研讨 内科 文献管理",
    "icu": "重症医学 重症医学科 文献日报 脓毒症 ARDS 机械通气 ECMO 器官支持 医学速递 循证医学 PubMed 病例研讨 内科 文献管理",
    "oncology": "医学文献 肿瘤 肿瘤科 文献日报 免疫治疗 靶向治疗 化疗 医学速递 循证医学 PubMed 病例研讨 内科 文献管理",
}

# 品牌固定配色（已上架/已验证科）；其余科按色相自动分配（确定性）
FIXED_PALETTES = {
    "rheumatology": {"bg": ("#0a1320", "#10223a", "#0b1726"), "pulse2": "#f0b429"},
    "respirology": {"bg": ("#07131f", "#0c2a3f", "#081b2b"), "pulse2": "#4fd1c5"},
    "icu": {"bg": ("#1f0f0a", "#3f2412", "#2b1608"), "pulse2": "#ed8936"},
    "oncology": {"bg": ("#0d1a17", "#123a30", "#0a201b"), "pulse2": "#34d399"},
}

MASTER_BG = ("#0a1320", "#10223a", "#0b1726")  # digest/assets/icon.svg 母版基线（= rheumatology）
MASTER_PULSE = "#f0b429"

# 占位符覆盖的模板文本（scripts/ 为通用代码，不经替换；spec.json 完全生成）
TEXT_FILES = ["SKILL.md", "README.md"]


def _auto_palette(idx):
    """确定性色相分配：深色三段底 + 亮色脉搏线（相邻科色相间隔 37°）。"""
    hue = (idx * 37 % 360) / 360.0
    bg = []
    for lightness in (0.07, 0.15, 0.09):
        r, g, b = colorsys.hls_to_rgb(hue, lightness, 0.55)
        bg.append("#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))
    r, g, b = colorsys.hls_to_rgb(hue, 0.60, 0.75)
    pulse2 = "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
    return {"bg": tuple(bg), "pulse2": pulse2}


def _keywords(spec, key):
    if key in KEYWORDS_OVERRIDE:
        return KEYWORDS_OVERRIDE[key]
    dz = " ".join(d["zh"] for d in spec.get("diseases", [])[:6])
    return "医学文献 %s 文献日报 %s 医学速递 循证医学 PubMed 病例研讨 内科 文献管理" % (spec["zh"], dz)


def all_keys():
    return sorted(os.path.splitext(f)[0] for f in os.listdir(SPEC_DIR) if f.endswith(".json"))


def gen(key, idx):
    assert key in SLUGS, "SLUGS 缺少 %s 的包名缩写" % key
    snap_path = os.path.join(SPEC_DIR, "%s.json" % key)
    assert os.path.exists(snap_path), "specs/ 缺少 %s.json（从 server/app/scripts/specs 投影）" % key
    spec = json.load(open(snap_path, encoding="utf-8"))
    assert isinstance(spec.get("zh"), str) and spec["zh"], "specs/%s.json 缺少 zh" % key
    assert isinstance(spec.get("diseases"), list) and spec["diseases"], "specs/%s.json 缺少 diseases" % key

    zh = spec["zh"]
    slug = "meddaily-" + SLUGS[key] + "-digest"
    name = "医学文献日报-%s" % zh
    quote = QUOTES.get(key)
    pal = FIXED_PALETTES.get(key) or _auto_palette(idx)

    dest = os.path.join(DEST_BASE, "%s-%s" % (slug, VERSION))
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(SRC, dest,
                    ignore=shutil.ignore_patterns("__pycache__", "data", "outputs", ".git",
                                                  ".gitignore", ".DS_Store"))
    os.makedirs(os.path.join(dest, "data"))
    os.makedirs(os.path.join(dest, "outputs"))
    os.makedirs(os.path.join(dest, "scripts", "specs"))

    # 1) 无 quote 的科：删除 README 的"真实产出示例"块
    if not quote:
        p = os.path.join(dest, "README.md")
        s = open(p, encoding="utf-8").read()
        s2 = re.sub(r"\*\*真实产出示例\*\*（2026-11-09）：\n\n> \{\{QUOTE1\}\}\n\n> \{\{QUOTE2\}\}\n\n"
                    r"完整样例随运行生成于 `outputs/`。\n\n", "", s)
        assert s2 != s, "README.md 示例块删除失败（模板已变）"
        open(p, "w", encoding="utf-8").write(s2)

    # 2) 占位符替换（逐文件断言命中 + 无残留）
    diseases = spec["diseases"]
    tokens = {
        "{{SLUG}}": slug,
        "{{VERSION}}": VERSION,
        "{{NAME}}": name,
        "{{DEPT}}": zh,
        "{{SPEC_FILE}}": "%s.json" % key,
        "{{DISEASE_COUNT}}": str(len(diseases)),
        "{{DISEASE_LIST}}": "、".join("%s（%s）" % (d["zh"], d["key"]) for d in diseases),
        "{{KEYWORDS}}": _keywords(spec, key),
    }
    if quote:
        tokens["{{QUOTE1}}"] = quote[0]
        tokens["{{QUOTE2}}"] = quote[1]
    EXPECT = {
        "{{SLUG}}": ["SKILL.md", "README.md"],
        "{{NAME}}": ["SKILL.md", "README.md"],
        "{{DEPT}}": ["SKILL.md", "README.md"],
        "{{SPEC_FILE}}": ["SKILL.md", "README.md"],
        "{{DISEASE_COUNT}}": ["SKILL.md", "README.md"],
        "{{VERSION}}": ["SKILL.md"],
        "{{DISEASE_LIST}}": ["README.md"],
        "{{KEYWORDS}}": ["README.md"],
    }
    if quote:
        EXPECT["{{QUOTE1}}"] = ["README.md"]
        EXPECT["{{QUOTE2}}"] = ["README.md"]
    per_file = {}
    for rel in TEXT_FILES:
        p = os.path.join(dest, rel)
        s = open(p, encoding="utf-8").read()
        per_file[rel] = {}
        for t, v in tokens.items():
            per_file[rel][t] = s.count(t)
            s = s.replace(t, v)
        assert "{{" not in s, "%s 残留未替换占位符" % rel
        open(p, "w", encoding="utf-8").write(s)
    for t, files in EXPECT.items():
        for rel in files:
            assert per_file[rel][t] >= 1, "模板 %s 缺少占位符 %s（逐文件断言）" % (rel, t)

    # 版本与版本记录（README）必须同步
    rd = open(os.path.join(dest, "README.md"), encoding="utf-8").read()
    assert ("### %s（" % VERSION) in rd, "README.md 缺少 %s 版本条目——先写版本记录再生成" % VERSION

    # 3) spec 放置与 spec.json
    shutil.copy(snap_path, os.path.join(dest, "scripts", "specs", "%s.json" % key))
    sp = {"name": name, "version": VERSION, "specialty_spec": "%s.json" % key,
          "key": key, "dept": zh, "file_prefix": slug}
    with open(os.path.join(dest, "spec.json"), "w", encoding="utf-8") as f:
        json.dump(sp, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # 4) 图标配色（svg 换色；SkillHub CLI 文件白名单不含 png，包内只保留 svg）
    svg_path = os.path.join(dest, "assets", "icon.svg")
    svg = open(svg_path, encoding="utf-8").read()
    for old, new in zip(MASTER_BG, pal["bg"]):
        svg = svg.replace(old, new)
    svg = svg.replace(MASTER_PULSE, pal["pulse2"])
    open(svg_path, "w", encoding="utf-8").write(svg)

    # 5) 语法检查 + 权限归一 + 打 zip
    for f in os.listdir(os.path.join(dest, "scripts")):
        if f.endswith(".py"):
            subprocess.run([sys.executable, "-m", "py_compile", os.path.join(dest, "scripts", f)], check=True)
    shutil.rmtree(os.path.join(dest, "scripts", "__pycache__"), ignore_errors=True)
    for root, dirs, files in os.walk(dest):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o755)
        for fn in files:
            os.chmod(os.path.join(root, fn), 0o755 if fn.endswith((".py", ".sh")) else 0o644)
    zip_path = os.path.join(DEST_BASE, "%s-%s.zip" % (slug, VERSION))
    if os.path.exists(zip_path):
        os.remove(zip_path)

    def add(zf, path, rel):
        if os.path.isdir(path):
            zf.write(path, rel + "/")
            for nm in sorted(os.listdir(path)):
                add(zf, os.path.join(path, nm), rel + "/" + nm)
        else:
            zf.write(path, rel)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        add(zf, dest, os.path.relpath(dest, DEST_BASE))
    print("✅ %-14s（%s）→ %s" % (slug, name, os.path.basename(zip_path)))


if __name__ == "__main__":
    keys = all_keys()
    for k in (sys.argv[1:] or keys):
        gen(k, keys.index(k) if k in keys else 0)
