"""Passive capture around the unchanged production forecasting pipeline.

No candidate model calls occur here. Only a successful, readback-verified
production forecast can be exported, encrypted, for the separate observer.
"""
from __future__ import annotations
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from functools import wraps
from shadow_comparison import EXPERIMENT_ID, ENDS_AT, probabilities, request_for

REPO = "mcabejsek-creator/metac-bot-template"
ARTIFACT = "vrchcab-shadow-input-v1"
CAPTURE_FILE = Path(".shadow/capture.enc")


def cipher(secret: str):
    from cryptography.fernet import Fernet
    if not isinstance(secret, str) or len(secret) < 20 or any(x.isspace() for x in secret):
        raise ValueError("shadow_encryption_key_unavailable")
    key = hashlib.sha256(("vrchcab-shadow-encryption-v1\0" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def utc_text(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid_shadow_timestamp")
    return value.astimezone(timezone.utc).isoformat()


class Capture:
    def __init__(self):
        self.records = {}
        self.originals = {}

    def attach(self, bot):
        for name in ("_binary_prompt_to_forecast", "_multiple_choice_prompt_to_forecast"):
            original = getattr(bot, name)
            self.originals[name] = original
            async def wrapped(question, prompt, _original=original):
                try:
                    self.observe(bot, question, prompt)
                except Exception:
                    logging.getLogger(__name__).warning("Shadow capture skipped; production is unchanged")
                return await _original(question, prompt)
            setattr(bot, name, wraps(original)(wrapped))

    def detach(self, bot):
        for name, method in self.originals.items():
            setattr(bot, name, method)

    def observe(self, bot, q, prompt):
        kind = q.question_type
        if kind not in ("binary", "multiple_choice"):
            return
        now = datetime.now(timezone.utc)
        if now >= ENDS_AT:
            return
        options = ["Yes", "No"] if kind == "binary" else list(q.options)
        request_for(prompt, options)
        ident = (q.id_of_post, q.id_of_question)
        if any(type(x) is not int or x <= 0 for x in ident):
            raise ValueError("invalid_capture_identity")
        if ident in self.records:
            if self.records[ident]["prompt"] != prompt:
                self.records[ident]["inconsistent"] = True
            return
        self.records[ident] = {"experiment": EXPERIMENT_ID, "post_id": ident[0], "question_id": ident[1],
            "question_type": kind, "options": options, "prompt": prompt,
            "captured_at": utc_text(now), "opens_at": utc_text(q.open_time),
            "closes_at": utc_text(bot._get_effective_close_time(q)), "inconsistent": False}

    def export(self, reports, environ=None):
        env = os.environ if environ is None else environ
        if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPO
                or env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_RUN_ATTEMPT") != "1"):
            return False
        if len(reports) != 1 or isinstance(reports[0], BaseException):
            return False
        report = reports[0]
        q = report.question
        item = self.records.get((q.id_of_post, q.id_of_question))
        if not item or item["inconsistent"]:
            return False
        if item["question_type"] == "binary":
            p = report.prediction
            baseline = probabilities([p, 1-p], 2)
        else:
            opts = report.prediction.predicted_options
            lookup = {x.option_name: x.probability for x in opts}
            if len(lookup) != len(opts) or set(lookup) != set(item["options"]):
                return False
            baseline = probabilities([lookup[x] for x in item["options"]], len(lookup))
        from automatic_model import AUTOMATIC_MODEL_ID, AUTOMATIC_MODEL_VERSION
        item = dict(item, baseline=baseline, baseline_model=AUTOMATIC_MODEL_ID,
                    baseline_version=AUTOMATIC_MODEL_VERSION,
                    baseline_verified_at=utc_text(datetime.now(timezone.utc)),
                    run_id=env["GITHUB_RUN_ID"], head_sha=env["GITHUB_SHA"], repository=REPO)
        encrypted = cipher(env.get("METACULUS_TOKEN", "")).encrypt(
            json.dumps(item, ensure_ascii=False, allow_nan=False).encode())
        CAPTURE_FILE.parent.mkdir(exist_ok=True)
        CAPTURE_FILE.write_bytes(encrypted)
        return True


def main():
    if sys.argv[1:] != ["--mode", "tournament", "--automatic-one"]:
        raise SystemExit("Shadow capture launcher accepts automatic tournament mode only")
    import main as production
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    production.check_environment(strict=True)
    production.print_startup_banner("tournament", will_publish=True)
    bot = production.build_template_bot(automatic_one=True)
    capture = Capture()
    try:
        capture.attach(bot)
    except Exception:
        capture.detach(bot)
        logging.getLogger(__name__).warning("Shadow instrumentation unavailable; continuing production")
    try:
        reports = asyncio.run(production.cancellable_cli(production.forecast_for_mode(
            bot, "tournament", automatic_one=True)))
    finally:
        capture.detach(bot)
    try:
        saved = capture.export(reports)
        logging.getLogger(__name__).info("Shadow encrypted capture available: %s", saved)
    except Exception:
        logging.getLogger(__name__).warning("Shadow export unavailable; production outcome unchanged")
    bot.log_report_summary(reports)
    production.print_run_summary_banner(reports, will_publish=True,
        tournament_url="https://www.metaculus.com/tournament/fall-futureeval-2026/")
    if any(isinstance(report, BaseException) for report in reports):
        raise SystemExit(1)

if __name__ == "__main__":
    main()
