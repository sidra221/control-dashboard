"""قياس زمن استجابة محرّك v2: محادثة ثابتة من 8 أدوار بالسعودي، N مرة، كل مرة على متجر مؤقت جديد.

    python manage.py bench_v2 --runs 3 --label before --out before.json
    python manage.py bench_v2 --runs 3 --label after --out after.json --analyzer-model gpt-5-mini
    python manage.py bench_v2 --compare before.json after.json

بيقيس لكل دور:
  - client_s   زمن الجدار حول طلب HTTP (اللي بيحسّه الزبون)
  - trace_ms   TurnTrace.latency_ms (من جوا السيرفر) و llm_calls
  - stage      TurnTrace.analyzer["timings"] لو موجود (analyzer_ms, seller_ms, total_ms, steps) — بنتحمّل غيابه
  - analyzer_call_s / seller_call_s  زمن كل استدعاء LLM منفرداً: wrapper حول llm.chat_with_retry و
    llm.chat_step_with_retry بيسجّل المدّة؛ analyzer = force_tool == "update_customer_profile"، والباقي بائع.

بيطبع جدول p50/p95/mean/min/max لكل دور وإجمالي، وبيكتب JSON (--out). --compare a.json b.json بيطبع جدول
قبل/بعد مع نسبة التغيير (سالب = أسرع). بيستعمل الموديل الحقيقي المضبوط (أو --provider/--model/--effort)،
فبيكلّف استدعاءات LLM حقيقية: تقريباً 2-4 استدعاءات لكل دور × 8 أدوار × N.
"""

import argparse
import json
import time
from contextlib import contextmanager
from unittest.mock import patch

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apitokens.evals import describe, v2_scenarios
from apitokens.management.commands.eval_v2 import Widget, eval_environment, llm_overrides
from apitokens.models import Conversation, TurnTrace
from apitokens.services import llm

ANALYZER_TOOL_NAME = "update_customer_profile"

# المحادثة الثابتة: (وصف الدور, رسالة الزبون)
BENCH_TURNS = [
    ("greeting", "هلا والله"),
    ("need_budget", "أبي فستان لعرس، وميزانيتي ما تتعدى 300 ريال"),
    ("card", "وريني الخيارات المتوفرة"),
    ("objection", "حلو، بس السعر عالي شوي علي"),
    ("alternative", "طيب وريني بديل أرخص"),
    ("size", "مقاسي M، متوفر؟"),
    ("buy_intent", "عجبني، أبي أطلبه الحين"),
    ("shipping", "وكم الشحن وكم يوم يوصل؟"),
]

STAGE_KEYS = ("analyzer_ms", "seller_ms", "total_ms")


# ======================================================================
# مؤقّت استدعاءات LLM
# ======================================================================

class LlmTimer:
    """wrapper بيسجّل مدّة كل llm.chat_with_retry / llm.chat_step_with_retry، موسوم analyzer/seller."""

    def __init__(self):
        self.calls = []

    def mark(self):
        return len(self.calls)

    def since(self, mark):
        return self.calls[mark:]

    @contextmanager
    def installed(self):
        orig_chat, orig_step = llm.chat_with_retry, llm.chat_step_with_retry

        def chat(*args, **kwargs):
            force = kwargs.get("force_tool", args[4] if len(args) > 4 else None)
            kind = "analyzer" if force == ANALYZER_TOOL_NAME else "seller"
            started = time.perf_counter()
            try:
                return orig_chat(*args, **kwargs)
            finally:
                self.calls.append({"kind": kind, "fn": "chat_with_retry", "seconds": time.perf_counter() - started})

        def step(*args, **kwargs):
            started = time.perf_counter()
            try:
                return orig_step(*args, **kwargs)
            finally:
                self.calls.append({"kind": "seller", "fn": "chat_step_with_retry",
                                   "seconds": time.perf_counter() - started})

        with patch.object(llm, "chat_with_retry", chat), patch.object(llm, "chat_step_with_retry", step):
            yield self


# ======================================================================
# تشغيل + إحصاء
# ======================================================================

def run_bench(runs, timer, label_prefix="bench"):
    """بيرجّع قائمة runs؛ كل run = قائمة سجلات أدوار. لازم يشتغل جوا eval_environment."""
    all_runs = []
    for run_index in range(runs):
        merchant = v2_scenarios.build_merchant(label=f"{label_prefix}-{run_index}")
        widget = Widget(merchant, ip=f"10.8.{run_index % 250}.1")
        turns = []
        for index, (label, text) in enumerate(BENCH_TURNS, start=1):
            mark = timer.mark()
            data, status, elapsed = widget.say(text)
            calls = timer.since(mark)
            trace = None
            if data.get("session_id") and data.get("turn"):
                conv = Conversation.objects.filter(merchant=merchant, session_id=data["session_id"]).first()
                trace = TurnTrace.objects.filter(conversation=conv, turn=data["turn"]).order_by("-id").first() if conv else None
            timings = ((trace.analyzer or {}).get("timings") or {}) if trace else {}
            turns.append({
                "index": index, "label": label, "user": text, "ok": bool(data.get("ok")), "status": status,
                "client_s": round(elapsed, 4), "trace_ms": trace.latency_ms if trace else None,
                "llm_calls": trace.llm_calls if trace else None,
                "timings": timings if isinstance(timings, dict) else {},
                "llm": [{"kind": c["kind"], "fn": c["fn"], "seconds": round(c["seconds"], 4)} for c in calls],
                "cards": len(data.get("recommendations") or []), "error": data.get("error"),
            })
        all_runs.append({"run": run_index, "turns": turns})
    return all_runs


def _llm_seconds(turn, kind):
    return sum(c["seconds"] for c in turn.get("llm", []) if c["kind"] == kind) or None


def _collect(turns):
    """قيم كل مقياس من قائمة أدوار (None بيتجاهل)."""
    def vals(fn):
        return [v for v in (fn(t) for t in turns) if v is not None]

    stage = {}
    for key in STAGE_KEYS:
        stage[key] = describe(vals(lambda t, k=key: (t.get("timings") or {}).get(k)))
    calls = vals(lambda t: t.get("llm_calls"))
    return {
        "client_s": describe(vals(lambda t: t.get("client_s"))),
        "trace_ms": describe(vals(lambda t: t.get("trace_ms"))),
        "analyzer_call_s": describe([c["seconds"] for t in turns for c in t.get("llm", []) if c["kind"] == "analyzer"]),
        "seller_call_s": describe([c["seconds"] for t in turns for c in t.get("llm", []) if c["kind"] == "seller"]),
        "analyzer_total_s": describe(vals(lambda t: _llm_seconds(t, "analyzer"))),
        "seller_total_s": describe(vals(lambda t: _llm_seconds(t, "seller"))),
        "stage": stage,
        "llm_calls_per_turn": (sum(calls) / len(calls)) if calls else None,
    }


def compute_stats(runs):
    """{"per_turn": [..], "overall": {..}} من نتائج run_bench."""
    flat = [t for r in runs for t in r["turns"]]
    per_turn = []
    for index, (label, _) in enumerate(BENCH_TURNS, start=1):
        stats = _collect([t for t in flat if t["index"] == index])
        per_turn.append({"index": index, "label": label, **stats})
    return {"per_turn": per_turn, "overall": _collect(flat)}


# المقاييس يلي بتنقارن بـ --compare: (اسم, مسار بالإحصاءات)
COMPARE_METRICS = [
    ("client_s", ("client_s",)), ("trace_ms", ("trace_ms",)),
    ("analyzer_call_s", ("analyzer_call_s",)), ("seller_call_s", ("seller_call_s",)),
    ("analyzer_ms (stage)", ("stage", "analyzer_ms")), ("seller_ms (stage)", ("stage", "seller_ms")),
    ("total_ms (stage)", ("stage", "total_ms")),
]


def _dig(stats, path):
    for key in path:
        stats = (stats or {}).get(key)
    return stats


def pct_delta(before, after):
    """نسبة التغيير المئوية (after-before)/before. None لو ما في قيمة أو before == 0."""
    if before is None or after is None or before == 0:
        return None
    return (after - before) / before * 100.0


def compare_reports(a, b):
    """جدول قبل/بعد: صفوف {"scope","metric","stat","before","after","delta_pct"} للإجمالي ولكل دور (p50/p95/mean)."""
    rows = []
    scopes = [("overall", a["stats"]["overall"], b["stats"]["overall"])]
    for ta, tb in zip(a["stats"]["per_turn"], b["stats"]["per_turn"]):
        scopes.append((f"turn {ta['index']} {ta['label']}", ta, tb))
    for scope, sa, sb in scopes:
        for metric, path in COMPARE_METRICS:
            da, db = _dig(sa, path), _dig(sb, path)
            if not da or not db or not da.get("n") or not db.get("n"):
                continue
            for stat in ("p50", "p95", "mean"):
                rows.append({"scope": scope, "metric": metric, "stat": stat, "before": da[stat], "after": db[stat],
                             "delta_pct": pct_delta(da[stat], db[stat])})
        ca, cb = sa.get("llm_calls_per_turn"), sb.get("llm_calls_per_turn")
        if ca is not None and cb is not None:
            rows.append({"scope": scope, "metric": "llm_calls_per_turn", "stat": "mean", "before": ca, "after": cb,
                         "delta_pct": pct_delta(ca, cb)})
    return rows


def _f(value, spec=".2f"):
    return "-" if value is None else format(value, spec)


def format_stats_table(stats):
    """جدول نصي: لكل دور p50/p95/mean/min/max لزمن العميل (ثواني) + زمن استدعاءات LLM."""
    lines = [f"{'turn':<16}{'p50':>8}{'p95':>8}{'mean':>8}{'min':>8}{'max':>8}   {'llm calls':>9}  {'analyzer p50':>12}  {'seller p50':>10}"]
    rows = [(f"{t['index']} {t['label']}", t) for t in stats["per_turn"]] + [("ALL TURNS", stats["overall"])]
    for name, t in rows:
        c = t["client_s"]
        lines.append(
            f"{name:<16}{_f(c['p50']):>8}{_f(c['p95']):>8}{_f(c['mean']):>8}{_f(c['min']):>8}{_f(c['max']):>8}   "
            f"{_f(t.get('llm_calls_per_turn'), '.1f'):>9}  {_f(t['analyzer_call_s']['p50']):>12}  {_f(t['seller_call_s']['p50']):>10}"
        )
    stage = stats["overall"]["stage"]
    if any(stage[k]["n"] for k in STAGE_KEYS):
        lines.append("stage timings (ms, from TurnTrace.analyzer['timings']): " + "  ".join(
            f"{k}: p50={_f(stage[k]['p50'], '.0f')} p95={_f(stage[k]['p95'], '.0f')}" for k in STAGE_KEYS))
    else:
        lines.append("stage timings: not present in TurnTrace.analyzer['timings'] (engine does not record them yet)")
    return "\n".join(lines)


def format_compare_table(rows, label_a="before", label_b="after"):
    lines = [f"{'scope':<24}{'metric':<22}{'stat':<6}{label_a:>12}{label_b:>12}{'delta':>10}"]
    for r in rows:
        delta = "-" if r["delta_pct"] is None else f"{r['delta_pct']:+.1f}%"
        lines.append(f"{r['scope']:<24}{r['metric']:<22}{r['stat']:<6}{_f(r['before'], '.3f'):>12}{_f(r['after'], '.3f'):>12}{delta:>10}")
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Latency benchmark for the v2 engine: fixed 8-turn Saudi conversation, N runs, with a before/after compare mode"

    def add_arguments(self, parser):
        parser.add_argument("--runs", type=int, default=3)
        parser.add_argument("--provider")
        parser.add_argument("--model")
        parser.add_argument("--effort")
        parser.add_argument("--analyzer-model", help="Override ANALYZER_MODEL")
        parser.add_argument("--out", help="Write the JSON report here")
        parser.add_argument("--label", default="run", help="Free label stored in the report (e.g. before/after)")
        parser.add_argument("--compare", nargs=2, metavar=("A_JSON", "B_JSON"), help="Print a before/after table and exit")
        parser.add_argument("--use-current-db", action="store_true", help=argparse.SUPPRESS)

    def handle(self, *args, **opts):
        if opts["compare"]:
            return self._compare(*opts["compare"])
        runs = max(1, opts["runs"])
        timer = LlmTimer()
        with llm_overrides(opts["provider"], opts["model"], opts["effort"], opts["analyzer_model"]):
            try:
                model = getattr(llm.get_provider(), "model", "") or ""
            except Exception:  # noqa: BLE001
                model = ""
            meta = {
                "label": opts["label"], "provider": settings.LLM_PROVIDER, "model": model,
                "analyzer_model": settings.ANALYZER_MODEL or None, "effort": settings.OPENAI_REASONING_EFFORT or None,
                "runs": runs, "turns": [{"label": lbl, "text": txt} for lbl, txt in BENCH_TURNS],
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.stdout.write(f"bench_v2 label={meta['label']} provider={meta['provider']} model={model} "
                              f"effort={meta['effort'] or '-'} analyzer_model={meta['analyzer_model'] or '-'} runs={runs}\n")
            with eval_environment(use_current_db=opts["use_current_db"]):
                with timer.installed():
                    results = run_bench(runs, timer)
        failed = [t for r in results for t in r["turns"] if not t["ok"]]
        report = {"label": opts["label"], "meta": meta, "runs": results, "stats": compute_stats(results)}
        self.stdout.write(format_stats_table(report["stats"]))
        if failed:
            self.stdout.write(self.style.WARNING(
                f"\n{len(failed)} turn(s) failed (errors: {sorted({str(t['error']) for t in failed})}); timings include them"))
        if opts["out"]:
            with open(opts["out"], "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            self.stdout.write(f"\nreport written: {opts['out']}")
        if failed:  # أزمنة فيها أدوار فاشلة مضلّلة: exit code 1 (الجدول والـ JSON انكتبوا مع ذلك)
            raise CommandError(f"{len(failed)} benchmark turn(s) failed; timings are not trustworthy", returncode=1)

    def _compare(self, path_a, path_b):
        reports = []
        for path in (path_a, path_b):
            try:
                with open(path, encoding="utf-8") as fh:
                    reports.append(json.load(fh))
            except (OSError, ValueError) as exc:
                raise CommandError(f"cannot read {path}: {exc}") from exc
        a, b = reports
        for report, path in ((a, path_a), (b, path_b)):
            if "stats" not in report:
                raise CommandError(f"{path} is not a bench_v2 report")
        rows = compare_reports(a, b)
        self.stdout.write(f"A = {a.get('label')} ({a['meta'].get('model')}, {a['meta'].get('runs')} runs)   "
                          f"B = {b.get('label')} ({b['meta'].get('model')}, {b['meta'].get('runs')} runs)")
        self.stdout.write("delta = (B - A) / A; negative means B is faster\n")
        self.stdout.write(format_compare_table(rows, a.get("label") or "before", b.get("label") or "after"))
        return None
