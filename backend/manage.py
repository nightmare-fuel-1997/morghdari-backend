#!/usr/bin/env python
"""Django's command-line utility — thin wrapper, nothing custom lives here.

فارسی: نقطهٔ ورود استاندارد جنگو؛ هیچ منطق سفارشی‌ای اینجا نیست.
The ONLY interesting line is default_settings_module: local runs default to
development; containers override via DJANGO_SETTINGS_MODULE env var so a
single image serves dev/test/prod (Implementation Blueprint §1).
"""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        # Missing Django almost always means the venv was not activated —
        # fail with an actionable message instead of a traceback wall.
        raise ImportError(
            "Django not found. Activate the venv: backend/.venv/Scripts/activate"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
