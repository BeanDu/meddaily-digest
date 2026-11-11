"""科室配置加载器（医学文献日报 · 多科室共用）

一份代码跑多个科室：把科室特异的检索词、期刊权重、prompt 角色、
标题/标签/文案等抽成 specs/<name>.json，脚本用 --spec 指定。

默认科室取自包根 spec.json 的 specialty_spec 字段（生成器写入，单一事实源），
可用 --spec 或 CLINICAL_SPEC 覆盖。

用法：
    import sys, os
    _HERE = os.path.dirname(os.path.abspath(__file__))
    from spec import load
    SPEC = load(args.spec)
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_DIR = os.path.join(_HERE, "specs")
_PKG = os.path.dirname(_HERE)


def _default_spec_name():
    """默认科室：包根 spec.json 的 specialty_spec；缺失时回退到包内唯一可用配置。"""
    try:
        with open(os.path.join(_PKG, "spec.json"), encoding="utf-8") as f:
            v = json.load(f).get("specialty_spec") or ""
        if v:
            return os.path.splitext(str(v))[0]
    except Exception:
        pass
    specs = list_specs()
    return specs[0] if specs else ""  # 空值仅用于无法定位时的报错提示


DEFAULT_SPEC = _default_spec_name()

_cache = {}


def spec_path(name):
    return os.path.join(SPEC_DIR, "%s.json" % name)


def list_specs():
    if not os.path.isdir(SPEC_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(SPEC_DIR)
        if f.endswith(".json")
    )


def load(name=None):
    """加载科室配置。name 为 None 时读环境变量 CLINICAL_SPEC，再退到包内默认科室 DEFAULT_SPEC。"""
    name = name or os.environ.get("CLINICAL_SPEC") or DEFAULT_SPEC
    if name in _cache:
        return _cache[name]
    p = spec_path(name)
    if not os.path.exists(p):
        raise SystemExit(
            "找不到科室配置: %s\n可用: %s" % (p, ", ".join(list_specs()))
        )
    with open(p, encoding="utf-8") as f:
        try:
            spec = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(
                "科室配置不是合法 JSON: %s\n  第 %d 行第 %d 列: %s\n  请检查逗号/引号后重试"
                % (p, e.lineno, e.colno, e.msg)
            )
    _cache[name] = spec
    return spec


def add_spec_arg(ap, help_text="科室配置名（默认 %s）" % DEFAULT_SPEC):
    """给 argparse 挂上 --spec 参数。"""
    ap.add_argument(
        "--spec",
        default=None,
        help=help_text,
    )
    return ap
