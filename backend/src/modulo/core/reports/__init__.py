"""Report generators and scheduler.

Registers built-in report generators on import. The scheduler module
is loaded by Celery via ``include``.
"""

from modulo.core.reports import quality_report  # noqa: F401
from modulo.core.reports.scheduler import register_report_type


def _register_quality_report() -> None:
    from modulo.core.reports.quality_report import (
        deliver_quality_report,
        format_slack_message,
        generate_quality_report,
    )

    register_report_type(
        report_type="quality",
        generator=generate_quality_report,
        formatter=format_slack_message,
        deliverer=deliver_quality_report,
    )


_register_quality_report()
