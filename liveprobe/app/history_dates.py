"""Calendar days shown to the operator use Korea Standard Time in both products."""
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))


def bounds(start: date | None, end: date | None):
    if start and end and start > end:
        raise ValueError("종료일은 시작일 이후여야 합니다.")
    if start == date.min or end == date.max:
        raise ValueError("날짜가 허용 범위를 벗어났습니다.")
    lower = datetime.combine(start, time.min, KST).astimezone(timezone.utc) if start else None
    upper = datetime.combine(end + timedelta(days=1), time.min, KST).astimezone(timezone.utc) if end else None
    return lower, upper
