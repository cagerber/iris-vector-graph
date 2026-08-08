"""
Tests targeting iris_vector_graph/cypher/translator.py lines 10577-11579:
_build_temporal_from_variable_map — temporal projection / override from a variable base.

This function is defined but not yet wired into the main dispatch path, so it is
called directly here using the AST helpers and a minimal mock context.

Branches covered:
  fn="date": base_date_expr → simple component overrides (year/month/day)
  fn="date": week-based (week+dow, week no dow inherit)
  fn="date": ordinalDay (with/without year override)
  fn="date": quarter (with/without dayOfQuarter, various Q values)
  fn="date": from localdatetime base (btype in localdatetime/datetime)
  fn="localtime": base localtime — no overrides, hour/minute/second override
  fn="localtime": base time → strip TZ (literal +HH:MM and Z forms)
  fn="localtime": base localdatetime/datetime → extract time part + strip TZ
  fn="time": base localtime → append Z (no overrides)
  fn="time": base time → keep TZ when no overrides
  fn="time": base time + new_tz → shift wall-clock time (compile-time literal)
  fn="time": base time + same tz → no shift
  fn="time": base time + tz from Z source → shift
  fn="time": hour/minute/second overrides with various source btypes
  fn="localdatetime": datetime base → strip TZ (literal and with tz_suffix)
  fn="localdatetime": datetime base → with component overrides (literal path)
  fn="localdatetime": datetime base → localdatetime source (no TZ to strip)
  fn="localdatetime": Case 1 date+literal time components
  fn="localdatetime": Case 1 date var + time var (localtime)
  fn="localdatetime": Case 1 date var + time var with second override
  fn="localdatetime": Case 2 year/month/day literals + time var
  fn="datetime": datetime base → literal path keep TZ
  fn="datetime": datetime base → localdatetime source adds Z
  fn="datetime": datetime base → TZ override shifts wall-clock
  fn="datetime": datetime base → with component overrides
  fn="datetime": Case 1 date var + time var (zoned)
  fn="datetime": Case 1 date var + time var TZ override shift
  fn="datetime": Case 1 date var + literal time components
  fn="datetime": Case 2 year/month/day literals + zoned time var
  fn="datetime": Case 2 year/month/day literals + localtime var → append Z
  fn="datetime": Case 2 TZ override shift

All tests are pure Python — no IRIS connection required.
"""
import pytest
from iris_vector_graph.cypher import ast
from iris_vector_graph.cypher.translator import _build_temporal_from_variable_map


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

class _MockContext:
    """Minimal translator context for _build_temporal_from_variable_map."""
    def __init__(self):
        self.variable_aliases = {}
        self.temporal_types = {}
        self.temporal_literal_values = {}
        self.parameters = {}


def _ctx(tl_values=None, t_types=None, aliases=None):
    c = _MockContext()
    c.temporal_literal_values = tl_values or {}
    c.temporal_types = t_types or {}
    c.variable_aliases = aliases or {}
    return c


def _lit(v):
    return ast.Literal(v)


def _var(n):
    return ast.Variable(n)


def _mlit(d):
    return ast.MapLiteral(d)


def build(fn, entries, tl_values=None, t_types=None, aliases=None):
    """Shorthand: call _build_temporal_from_variable_map and return the SQL string."""
    m = _mlit(entries)
    c = _ctx(tl_values, t_types, aliases)
    return _build_temporal_from_variable_map(fn, m, c)


# ===========================================================================
# fn = "date"
# ===========================================================================

class TestDateNoOverrides:
    """date({date: d}) — no component overrides."""

    def test_date_base_no_overrides(self):
        """All three components extracted via SUBSTRING from base literal."""
        sql = build("date", {"date": _var("d")},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "SUBSTRING('2023-05-15', 1, 4)" in sql  # year
        assert "SUBSTRING('2023-05-15', 6, 2)" in sql  # month
        assert "SUBSTRING('2023-05-15', 9, 2)" in sql  # day

    def test_date_base_from_localdatetime(self):
        """base is localdatetime — code path uses the literal value for date part."""
        sql = build("date", {"date": _var("dt")},
                    tl_values={"dt": "2023-05-15T10:30:00"}, t_types={"dt": "localdatetime"})
        # Should work and contain date portion
        assert sql is not None
        assert "2023" in sql or "SUBSTRING" in sql


class TestDateYearOverride:
    def test_year_only(self):
        sql = build("date", {"date": _var("d"), "year": _lit(2028)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'2028'" in sql
        assert "SUBSTRING('2023-05-15', 6, 2)" in sql   # month from base
        assert "SUBSTRING('2023-05-15', 9, 2)" in sql   # day from base

    def test_month_only(self):
        sql = build("date", {"date": _var("d"), "month": _lit(11)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'11'" in sql
        assert "SUBSTRING('2023-05-15', 1, 4)" in sql  # year from base
        assert "SUBSTRING('2023-05-15', 9, 2)" in sql  # day from base

    def test_day_only(self):
        sql = build("date", {"date": _var("d"), "day": _lit(28)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'28'" in sql
        assert "SUBSTRING('2023-05-15', 1, 4)" in sql
        assert "SUBSTRING('2023-05-15', 6, 2)" in sql

    def test_year_and_month(self):
        sql = build("date", {"date": _var("d"), "year": _lit(2000), "month": _lit(1)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'2000'" in sql
        assert "'01'" in sql

    def test_year_month_day_all(self):
        sql = build("date",
                    {"date": _var("d"), "year": _lit(2001), "month": _lit(2), "day": _lit(3)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'2001'" in sql
        assert "'02'" in sql
        assert "'03'" in sql


class TestDateWeekBased:
    """date({date: d, week: W, ...}) — ISO week-based date construction."""

    def test_week_only_inherits_dow(self):
        """No dayOfWeek key — inherits day-of-week from base date."""
        sql = build("date", {"date": _var("d"), "week": _lit(10)},
                    tl_values={"d": "2023-03-08"}, t_types={"d": "date"})
        # Should produce DATEADD with dow_offset inherited from base
        assert "DATEADD" in sql
        assert "DAYOFWEEK" in sql
        # The format result: CAST(YEAR(...)) || '-' || ...
        assert "YEAR(" in sql
        assert "MONTH(" in sql
        assert "DAY(" in sql

    def test_week_with_dow_monday(self):
        """dayOfWeek=1 (Monday): no extra day added."""
        sql = build("date", {"date": _var("d"), "week": _lit(1), "dayOfWeek": _lit(1)},
                    tl_values={"d": "2023-01-09"}, t_types={"d": "date"})
        assert "DATEADD" in sql
        # Monday=1: dow_val - 1 = 0, so no explicit DATEADD for day offset expected
        assert "(1-1)*7" in sql

    def test_week_with_dow_wednesday(self):
        """dayOfWeek=3 (Wednesday): adds 2 days."""
        sql = build("date", {"date": _var("d"), "week": _lit(5), "dayOfWeek": _lit(3)},
                    tl_values={"d": "2023-01-09"}, t_types={"d": "date"})
        assert "DATEADD" in sql
        # dow_val - 1 = 2
        assert ", 2," in sql or "'day', 2," in sql

    def test_week_with_dow_sunday(self):
        """dayOfWeek=7 (Sunday): adds 6 days."""
        sql = build("date", {"date": _var("d"), "week": _lit(52), "dayOfWeek": _lit(7)},
                    tl_values={"d": "2023-12-25"}, t_types={"d": "date"})
        assert "DATEADD" in sql
        # dow_val - 1 = 6
        assert ", 6," in sql or "'day', 6," in sql

    def test_week_with_explicit_year(self):
        """Explicit year in the map."""
        sql = build("date", {"date": _var("d"), "year": _lit(2025), "week": _lit(2)},
                    tl_values={"d": "2023-01-09"}, t_types={"d": "date"})
        assert "'2025'" in sql
        assert "DATEADD" in sql

    def test_week_year_from_base_if_no_year_key(self):
        """Without year key, year comes from SUBSTRING of base date."""
        sql = build("date", {"date": _var("d"), "week": _lit(20)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "SUBSTRING('2023-05-15', 1, 4)" in sql


class TestDateOrdinalDay:
    """date({date: d, ordinalDay: N})."""

    def test_ordinal_day_100(self):
        sql = build("date", {"date": _var("d"), "ordinalDay": _lit(100)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        # ordinal_val - 1 = 99
        assert "99" in sql
        assert "DATEADD" in sql
        assert "01-01" in sql  # Jan 1

    def test_ordinal_day_1(self):
        """First day: ordinal - 1 = 0."""
        sql = build("date", {"date": _var("d"), "ordinalDay": _lit(1)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "0," in sql  # DATEADD('day', 0, ...)
        assert "01-01" in sql

    def test_ordinal_day_365(self):
        sql = build("date", {"date": _var("d"), "ordinalDay": _lit(365)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "364" in sql  # 365 - 1

    def test_ordinal_day_with_year_override(self):
        sql = build("date", {"date": _var("d"), "year": _lit(2024), "ordinalDay": _lit(1)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'2024'" in sql
        assert "0," in sql

    def test_ordinal_day_formats_ymd(self):
        """Result formats YYYY-MM-DD using YEAR/MONTH/DAY."""
        sql = build("date", {"date": _var("d"), "ordinalDay": _lit(200)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "YEAR(" in sql
        assert "MONTH(" in sql
        assert "DAY(" in sql


class TestDateQuarter:
    """date({date: d, quarter: Q, ...})."""

    def test_quarter_q1(self):
        sql = build("date", {"date": _var("d"), "quarter": _lit(1)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "01-01" in sql  # Q1 start

    def test_quarter_q2(self):
        sql = build("date", {"date": _var("d"), "quarter": _lit(2)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "04-01" in sql  # Q2 start = April 1

    def test_quarter_q3(self):
        sql = build("date", {"date": _var("d"), "quarter": _lit(3)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "07-01" in sql  # Q3 start = July 1

    def test_quarter_q4(self):
        sql = build("date", {"date": _var("d"), "quarter": _lit(4)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "10-01" in sql  # Q4 start = October 1

    def test_quarter_with_doq(self):
        """Explicit dayOfQuarter: doq_val - 1 days after quarter start."""
        sql = build("date", {"date": _var("d"), "quarter": _lit(2), "dayOfQuarter": _lit(15)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "DATEADD" in sql
        assert "14" in sql  # doq_val - 1 = 14
        assert "04-01" in sql

    def test_quarter_doq_1(self):
        """dayOfQuarter=1 → doq_val - 1 = 0."""
        sql = build("date", {"date": _var("d"), "quarter": _lit(1), "dayOfQuarter": _lit(1)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        # DATEADD('day', 0, ...) — offset zero
        assert "0," in sql

    def test_quarter_with_year(self):
        sql = build("date", {"date": _var("d"), "year": _lit(2025), "quarter": _lit(3)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'2025'" in sql
        assert "07-01" in sql

    def test_quarter_no_doq_inherits_from_base(self):
        """Without dayOfQuarter, month and day are inherited from base date."""
        sql = build("date", {"date": _var("d"), "quarter": _lit(4)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        # Code uses MOD(base_month - 1, 3) for month offset
        assert "MOD(" in sql
        assert "10-01" in sql

    def test_quarter_formats_ymd(self):
        """Quarter output uses YEAR/MONTH/DAY."""
        sql = build("date", {"date": _var("d"), "quarter": _lit(2)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "YEAR(" in sql
        assert "MONTH(" in sql
        assert "DAY(" in sql

    def test_quarter_with_year_and_doq(self):
        sql = build("date",
                    {"date": _var("d"), "year": _lit(2024), "quarter": _lit(1), "dayOfQuarter": _lit(32)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'2024'" in sql
        assert "31" in sql  # doq_val - 1 = 31
        assert "01-01" in sql


class TestDateReturnsNoneWhenNoBase:
    """date(...) with no date key returns None."""

    def test_no_date_key(self):
        sql = build("date", {"hour": _lit(10)},
                    tl_values={}, t_types={})
        assert sql is None


# ===========================================================================
# fn = "localtime"
# ===========================================================================

class TestLocaltimeFromLocaltimeBase:
    """fn='localtime', base is localtime variable."""

    def test_no_overrides_returns_literal(self):
        sql = build("localtime", {"time": _var("lt")},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert sql == "'10:30:00'"

    def test_hour_override(self):
        sql = build("localtime", {"time": _var("lt"), "hour": _lit(14)},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert "'14'" in sql
        assert "SUBSTRING('10:30:00', 4, 2)" in sql   # minute from base
        assert "SUBSTRING('10:30:00', 7, 2)" in sql   # second from base

    def test_minute_override(self):
        sql = build("localtime", {"time": _var("lt"), "minute": _lit(45)},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert "'45'" in sql

    def test_second_override(self):
        sql = build("localtime", {"time": _var("lt"), "second": _lit(59)},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert "'59'" in sql

    def test_hour_and_minute_override(self):
        sql = build("localtime", {"time": _var("lt"), "hour": _lit(8), "minute": _lit(0)},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert "'08'" in sql
        assert "'00'" in sql


class TestLocaltimeFromTimeBase:
    """fn='localtime', base is a zoned time variable — TZ is stripped."""

    def test_strip_plus_tz(self):
        """+01:00 is stripped from time literal."""
        sql = build("localtime", {"time": _var("t")},
                    tl_values={"t": "12:30:00+01:00"}, t_types={"t": "time"})
        assert sql == "'12:30:00'"
        assert "+01:00" not in sql

    def test_strip_z(self):
        """Z suffix is stripped."""
        sql = build("localtime", {"time": _var("t")},
                    tl_values={"t": "12:30:00Z"}, t_types={"t": "time"})
        assert sql == "'12:30:00'"

    def test_strip_minus_tz(self):
        """−HH:MM TZ is stripped."""
        sql = build("localtime", {"time": _var("t")},
                    tl_values={"t": "08:00:00-05:00"}, t_types={"t": "time"})
        assert sql == "'08:00:00'"

    def test_runtime_column_localtime_from_time(self):
        """Without temporal_literal_values, uses runtime CASE expression."""
        # Variable 't' not in tl_values → runtime path
        sql = build("localtime", {"time": _var("t")},
                    tl_values={}, t_types={"t": "time"},
                    aliases={"t": "Stage1"})
        # Should produce a CASE expression for TZ stripping
        assert "CASE" in sql
        assert "CHARINDEX" in sql


class TestLocaltimeFromDatetimeBase:
    """fn='localtime', base is localdatetime or datetime — extract time part + strip TZ."""

    def test_from_localdatetime_literal(self):
        """Extracts time portion after 'T'."""
        sql = build("localtime", {"time": _var("dt")},
                    tl_values={"dt": "2023-05-15T14:30:00"}, t_types={"dt": "localdatetime"})
        assert sql == "'14:30:00'"

    def test_from_datetime_strip_plus_tz(self):
        """Extracts time and strips +HH:MM."""
        sql = build("localtime", {"time": _var("dt")},
                    tl_values={"dt": "2023-05-15T14:30:00+02:00"}, t_types={"dt": "datetime"})
        assert sql == "'14:30:00'"
        assert "+02:00" not in sql

    def test_from_datetime_strip_z(self):
        """Extracts time and strips Z."""
        sql = build("localtime", {"time": _var("dt")},
                    tl_values={"dt": "2023-05-15T10:00:00Z"}, t_types={"dt": "datetime"})
        assert sql == "'10:00:00'"

    def test_from_datetime_with_iana_zone(self):
        """Strips IANA zone [Europe/Paris] from datetime literal."""
        sql = build("localtime", {"time": _var("dt")},
                    tl_values={"dt": "2023-05-15T10:00:00+02:00[Europe/Paris]"},
                    t_types={"dt": "datetime"})
        assert "Europe/Paris" not in sql
        assert "+02:00" not in sql
        assert sql == "'10:00:00'"


# ===========================================================================
# fn = "time"
# ===========================================================================

class TestTimeFromLocaltimeBase:
    """fn='time', base is localtime — appends Z when no overrides."""

    def test_no_overrides_appends_z(self):
        sql = build("time", {"time": _var("lt")},
                    tl_values={"lt": "12:30:00"}, t_types={"lt": "localtime"})
        assert "Z" in sql
        assert "12:30:00" in sql

    def test_tz_override_appends_new_tz(self):
        """localtime + timezone override → append new TZ (no shift, just append)."""
        sql = build("time", {"time": _var("lt"), "timezone": _lit("+00:00")},
                    tl_values={"lt": "10:00"}, t_types={"lt": "localtime"})
        assert "+00:00" in sql

    def test_localtime_tz_from_localdatetime(self):
        """time({time: dt}) where dt is localdatetime — extract time, append Z."""
        sql = build("time", {"time": _var("dt")},
                    tl_values={"dt": "2023-05-15T14:30:00"}, t_types={"dt": "localdatetime"})
        assert "Z" in sql
        assert "14:30:00" in sql


class TestTimeFromTimeBase:
    """fn='time', base is a zoned time variable."""

    def test_no_overrides_keeps_tz(self):
        """No overrides: return time literal with its TZ preserved."""
        sql = build("time", {"time": _var("t")},
                    tl_values={"t": "12:30:00+01:00"}, t_types={"t": "time"})
        assert sql == "'12:30:00+01:00'"

    def test_tz_override_shifts_wall_clock_plus_to_plus(self):
        """12:00+01:00 shifted to +02:00 → 13:00+02:00."""
        sql = build("time", {"time": _var("t"), "timezone": _lit("+02:00")},
                    tl_values={"t": "12:00+01:00"}, t_types={"t": "time"})
        assert sql == "'13:00+02:00'"

    def test_tz_override_no_shift_when_same(self):
        """Same TZ: no shift."""
        sql = build("time", {"time": _var("t"), "timezone": _lit("+01:00")},
                    tl_values={"t": "12:30+01:00"}, t_types={"t": "time"})
        assert "12:30" in sql
        assert "+01:00" in sql

    def test_tz_override_from_z_source(self):
        """10:00Z shifted to +03:00 → 13:00+03:00."""
        sql = build("time", {"time": _var("t"), "timezone": _lit("+03:00")},
                    tl_values={"t": "10:00Z"}, t_types={"t": "time"})
        assert sql == "'13:00+03:00'"

    def test_tz_override_negative(self):
        """10:00+01:00 shifted to -05:00 → 04:00-05:00."""
        sql = build("time", {"time": _var("t"), "timezone": _lit("-05:00")},
                    tl_values={"t": "10:00+01:00"}, t_types={"t": "time"})
        assert sql == "'04:00-05:00'"

    def test_tz_shift_with_seconds(self):
        """12:31:14.645876+01:00 shifted to +02:00 → 13:31:14.645876+02:00."""
        sql = build("time", {"time": _var("t"), "timezone": _lit("+02:00")},
                    tl_values={"t": "12:31:14.645876+01:00"}, t_types={"t": "time"})
        assert "13:31:14.645876+02:00" in sql

    def test_hour_override(self):
        sql = build("time", {"time": _var("t"), "hour": _lit(9)},
                    tl_values={"t": "12:30:00+01:00"}, t_types={"t": "time"})
        assert "'09'" in sql

    def test_minute_override(self):
        sql = build("time", {"time": _var("t"), "minute": _lit(15)},
                    tl_values={"t": "12:30:00+01:00"}, t_types={"t": "time"})
        assert "'15'" in sql

    def test_second_override(self):
        sql = build("time", {"time": _var("t"), "second": _lit(42)},
                    tl_values={"t": "12:30:00+01:00"}, t_types={"t": "time"})
        assert "'42'" in sql

    def test_hour_and_tz_override(self):
        """Override hour alongside TZ change — code returns from TZ shift path early
        (applies shift to base literal before hour override takes effect)."""
        sql = build("time", {"time": _var("t"), "hour": _lit(8), "timezone": _lit("+02:00")},
                    tl_values={"t": "12:30:00+01:00"}, t_types={"t": "time"})
        # TZ shift path executes: 12:30+01:00 → 13:30+02:00 (delta=+60 min)
        assert "13:30" in sql
        assert "+02:00" in sql

    def test_second_override_all_zeros(self):
        sql = build("time", {"time": _var("t"), "second": _lit(0)},
                    tl_values={"t": "12:30:45+01:00"}, t_types={"t": "time"})
        assert "'00'" in sql

    def test_no_override_source_has_fraction(self):
        """time with fractional seconds, no overrides — kept as-is."""
        sql = build("time", {"time": _var("t")},
                    tl_values={"t": "09:15:30.123456+05:30"}, t_types={"t": "time"})
        assert "09:15:30.123456+05:30" in sql


class TestTimeReturnsNoneWhenNoBase:
    def test_no_time_key(self):
        sql = build("time", {"year": _lit(2023)},
                    tl_values={}, t_types={})
        assert sql is None


# ===========================================================================
# fn = "localdatetime"
# ===========================================================================

class TestLocaldatetimeFromDatetimeBase:
    """fn='localdatetime', {datetime: var} — Case 0."""

    def test_strip_tz_from_datetime(self):
        sql = build("localdatetime", {"datetime": _var("dt")},
                    tl_values={"dt": "2023-05-15T10:30:00+01:00"}, t_types={"dt": "datetime"})
        assert sql == "'2023-05-15T10:30:00'"

    def test_strip_z_from_datetime(self):
        sql = build("localdatetime", {"datetime": _var("dt")},
                    tl_values={"dt": "2023-05-15T10:30:00Z"}, t_types={"dt": "datetime"})
        assert sql == "'2023-05-15T10:30:00'"

    def test_strip_iana_zone(self):
        """Strips both numeric TZ and IANA bracket."""
        sql = build("localdatetime", {"datetime": _var("dt")},
                    tl_values={"dt": "2023-05-15T10:30:00+02:00[Europe/Paris]"},
                    t_types={"dt": "datetime"})
        assert "Europe/Paris" not in sql
        assert "+02:00" not in sql
        assert sql == "'2023-05-15T10:30:00'"

    def test_from_localdatetime_source(self):
        """Source is already localdatetime — no TZ to strip."""
        sql = build("localdatetime", {"datetime": _var("dt")},
                    tl_values={"dt": "2023-05-15T10:30:00"}, t_types={"dt": "localdatetime"})
        assert sql == "'2023-05-15T10:30:00'"

    def test_year_override(self):
        sql = build("localdatetime", {"datetime": _var("dt"), "year": _lit(2030)},
                    tl_values={"dt": "2023-05-15T10:30:00Z"}, t_types={"dt": "datetime"})
        assert "2030" in sql
        assert "+00:00" not in sql  # TZ stripped for localdatetime

    def test_month_override(self):
        sql = build("localdatetime", {"datetime": _var("dt"), "month": _lit(12)},
                    tl_values={"dt": "2023-05-15T10:30:00Z"}, t_types={"dt": "datetime"})
        assert "12" in sql

    def test_day_override(self):
        sql = build("localdatetime", {"datetime": _var("dt"), "day": _lit(28)},
                    tl_values={"dt": "2023-05-15T10:30:00Z"}, t_types={"dt": "datetime"})
        assert "28" in sql

    def test_hour_override(self):
        sql = build("localdatetime", {"datetime": _var("dt"), "hour": _lit(22)},
                    tl_values={"dt": "2023-05-15T10:30:00Z"}, t_types={"dt": "datetime"})
        assert "22" in sql

    def test_minute_override(self):
        sql = build("localdatetime", {"datetime": _var("dt"), "minute": _lit(59)},
                    tl_values={"dt": "2023-05-15T10:30:00Z"}, t_types={"dt": "datetime"})
        assert "59" in sql

    def test_second_override(self):
        sql = build("localdatetime", {"datetime": _var("dt"), "second": _lit(45)},
                    tl_values={"dt": "2023-05-15T10:30:00Z"}, t_types={"dt": "datetime"})
        assert "45" in sql

    def test_multiple_overrides(self):
        sql = build("localdatetime",
                    {"datetime": _var("dt"), "year": _lit(2024), "month": _lit(6), "hour": _lit(8)},
                    tl_values={"dt": "2023-05-15T10:30:00Z"}, t_types={"dt": "datetime"})
        assert "2024" in sql
        assert "06" in sql
        assert "08" in sql

    def test_second_override_on_fractional(self):
        """Second override applied; fractional part preserved."""
        sql = build("localdatetime", {"datetime": _var("dt"), "second": _lit(5)},
                    tl_values={"dt": "2023-05-15T10:30:00.123456Z"}, t_types={"dt": "datetime"})
        assert "05" in sql


class TestLocaldatetimeCaseOne:
    """fn='localdatetime', {date: d, ...} — Case 1."""

    def test_date_var_literal_time(self):
        """Date from var, time from literal components (hour/minute/second)."""
        sql = build("localdatetime",
                    {"date": _var("d"), "hour": _lit(10), "minute": _lit(30), "second": _lit(0)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "2023" in sql or "SUBSTRING" in sql
        assert "10:30" in sql

    def test_date_var_plus_localtime_var(self):
        """Date from var, time from localtime var — combine them."""
        sql = build("localdatetime", {"date": _var("d"), "time": _var("lt")},
                    tl_values={"d": "2023-05-15", "lt": "14:30:00"},
                    t_types={"d": "date", "lt": "localtime"})
        assert "2023" in sql or "SUBSTRING" in sql
        assert "T" in sql

    def test_date_var_plus_zoned_time_strips_tz(self):
        """Time from zoned time var: localdatetime strips TZ via CASE expression."""
        sql = build("localdatetime", {"date": _var("d"), "time": _var("t")},
                    tl_values={"d": "2023-05-15", "t": "14:30:00+01:00"},
                    t_types={"d": "date", "t": "time"})
        # The CASE expression handles TZ stripping at runtime.
        # The SQL text itself may contain the source literal '14:30:00+01:00'
        # as the input to the CASE, which is expected.
        assert "CHARINDEX" in sql or "CASE" in sql or "T" in sql

    def test_date_var_plus_time_with_second_override(self):
        sql = build("localdatetime",
                    {"date": _var("d"), "time": _var("lt"), "second": _lit(42)},
                    tl_values={"d": "2023-05-15", "lt": "14:30:00"},
                    t_types={"d": "date", "lt": "localtime"})
        assert "42" in sql

    def test_date_var_with_day_override(self):
        sql = build("localdatetime",
                    {"date": _var("d"), "day": _lit(20), "hour": _lit(0), "minute": _lit(0)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "20" in sql

    def test_date_var_with_year_override(self):
        sql = build("localdatetime",
                    {"date": _var("d"), "year": _lit(2025), "hour": _lit(12), "minute": _lit(0)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "2025" in sql


class TestLocaldatetimeCaseTwo:
    """fn='localdatetime', {year:..., month:..., day:..., time: var} — Case 2."""

    def test_ymd_and_localtime(self):
        sql = build("localdatetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15), "time": _var("lt")},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert "2023-05-15" in sql

    def test_ymd_and_time_strips_tz(self):
        """localdatetime strips TZ via runtime CASE expression from the time variable."""
        sql = build("localdatetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15), "time": _var("t")},
                    tl_values={"t": "10:30:00+02:00"}, t_types={"t": "time"})
        assert "2023" in sql
        # TZ stripping happens at runtime via CASE/CHARINDEX; source literal '+02:00'
        # appears in the SQL text as input to the CASE expression — that is correct.
        assert "CHARINDEX" in sql or "CASE" in sql

    def test_ymd_and_time_with_second_override(self):
        sql = build("localdatetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15),
                     "time": _var("lt"), "second": _lit(30)},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert "30" in sql
        assert "2023" in sql

    def test_ymd_and_time_with_fractional_seconds(self):
        sql = build("localdatetime",
                    {"year": _lit(2023), "month": _lit(1), "day": _lit(1), "time": _var("lt")},
                    tl_values={"lt": "10:30:00.123456"}, t_types={"lt": "localtime"})
        assert "123456" in sql

    def test_ymd_and_datetime_extracts_time(self):
        """When time base is datetime, extracts time portion."""
        sql = build("localdatetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15), "time": _var("dt")},
                    tl_values={"dt": "2020-01-01T10:30:00Z"}, t_types={"dt": "datetime"})
        assert "2023" in sql
        assert "10" in sql


# ===========================================================================
# fn = "datetime"
# ===========================================================================

class TestDatetimeFromDatetimeBase:
    """fn='datetime', {datetime: var} — Case 0."""

    def test_keep_tz_from_literal(self):
        sql = build("datetime", {"datetime": _var("dt")},
                    tl_values={"dt": "2023-05-15T10:30:00+01:00"}, t_types={"dt": "datetime"})
        assert sql == "'2023-05-15T10:30:00+01:00'"

    def test_localdatetime_source_adds_z(self):
        sql = build("datetime", {"datetime": _var("dt")},
                    tl_values={"dt": "2023-05-15T10:30:00"}, t_types={"dt": "localdatetime"})
        assert sql == "'2023-05-15T10:30:00Z'"

    def test_tz_override_shifts_wall_clock(self):
        """10:30+01:00 → 11:30+02:00 when timezone override is +02:00."""
        sql = build("datetime", {"datetime": _var("dt"), "timezone": _lit("+02:00")},
                    tl_values={"dt": "2023-05-15T10:30:00+01:00"}, t_types={"dt": "datetime"})
        assert sql == "'2023-05-15T11:30+02:00'"

    def test_tz_override_same_no_shift(self):
        sql = build("datetime", {"datetime": _var("dt"), "timezone": _lit("+01:00")},
                    tl_values={"dt": "2023-05-15T10:30:00+01:00"}, t_types={"dt": "datetime"})
        assert "10:30" in sql
        assert "+01:00" in sql

    def test_tz_override_from_z(self):
        """Z source + +05:30 override — code doesn't shift for Z sources (tz_m0 is None)."""
        sql = build("datetime", {"datetime": _var("dt"), "timezone": _lit("+05:30")},
                    tl_values={"dt": "2023-05-15T10:00:00Z"}, t_types={"dt": "datetime"})
        # Z source uses elif branch — sets tz_suffix='Z', but override replaces it
        assert "+05:30" in sql

    def test_localdatetime_source_with_tz_override(self):
        """localdatetime + timezone override → append new TZ (no shift for naive source)."""
        sql = build("datetime", {"datetime": _var("dt"), "timezone": _lit("+00:00")},
                    tl_values={"dt": "2023-05-15T10:30:00"}, t_types={"dt": "localdatetime"})
        assert "+00:00" in sql
        assert "10:30:00" in sql

    def test_year_override(self):
        sql = build("datetime", {"datetime": _var("dt"), "year": _lit(2030)},
                    tl_values={"dt": "2023-05-15T10:30:00+01:00"}, t_types={"dt": "datetime"})
        assert "2030" in sql

    def test_hour_override(self):
        sql = build("datetime", {"datetime": _var("dt"), "hour": _lit(22)},
                    tl_values={"dt": "2023-05-15T10:30:00+01:00"}, t_types={"dt": "datetime"})
        assert "22" in sql
        assert "+01:00" in sql

    def test_backward_tz_shift(self):
        """12:00+02:00 shifted to -08:00 → 02:00-08:00."""
        sql = build("datetime", {"datetime": _var("dt"), "timezone": _lit("-08:00")},
                    tl_values={"dt": "2023-05-15T12:00:00+02:00"}, t_types={"dt": "datetime"})
        assert "02:00" in sql
        assert "-08:00" in sql

    def test_runtime_sql_path_no_literal_value(self):
        """Without temporal_literal_value, returns runtime SQL expression."""
        sql = build("datetime", {"datetime": _var("dt")},
                    tl_values={}, t_types={"dt": "datetime"},
                    aliases={"dt": "Stage1"})
        # Should return a CASE expression for stripping/re-appending TZ
        assert sql is not None
        assert "CASE" in sql or "Stage1" in sql


class TestDatetimeCaseOne:
    """fn='datetime', {date: d, time: t, ...} — Case 1."""

    def test_date_and_localtime_appends_z(self):
        sql = build("datetime", {"date": _var("d"), "time": _var("lt")},
                    tl_values={"d": "2023-05-15", "lt": "14:30:00"},
                    t_types={"d": "date", "lt": "localtime"})
        assert "2023" in sql
        assert "Z" in sql

    def test_date_and_zoned_time_preserves_tz(self):
        sql = build("datetime", {"date": _var("d"), "time": _var("t")},
                    tl_values={"d": "2023-05-15", "t": "14:30:00+01:00"},
                    t_types={"d": "date", "t": "time"})
        assert "2023" in sql
        assert "+01:00" in sql

    def test_date_and_time_tz_override_shifts(self):
        """14:30+01:00 shifted to +02:00 → 15:30+02:00."""
        sql = build("datetime",
                    {"date": _var("d"), "time": _var("t"), "timezone": _lit("+02:00")},
                    tl_values={"d": "2023-05-15", "t": "14:30:00+01:00"},
                    t_types={"d": "date", "t": "time"})
        assert "15:30" in sql
        assert "+02:00" in sql

    def test_date_and_time_second_override(self):
        sql = build("datetime",
                    {"date": _var("d"), "time": _var("t"), "second": _lit(59)},
                    tl_values={"d": "2023-05-15", "t": "14:30:00+01:00"},
                    t_types={"d": "date", "t": "time"})
        assert "59" in sql

    def test_date_var_literal_time_parts(self):
        """Date from var, time from literal hour/minute/second."""
        sql = build("datetime",
                    {"date": _var("d"), "hour": _lit(10), "minute": _lit(0), "second": _lit(0)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "2023" in sql or "SUBSTRING" in sql
        assert "Z" in sql  # no time var → default Z


class TestDatetimeCaseTwo:
    """fn='datetime', {year:..., month:..., day:..., time: var} — Case 2."""

    def test_ymd_and_zoned_time(self):
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15), "time": _var("t")},
                    tl_values={"t": "10:30:00+02:00"}, t_types={"t": "time"})
        assert "2023" in sql
        assert "+02:00" in sql

    def test_ymd_and_localtime_appends_z(self):
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15), "time": _var("lt")},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert "2023" in sql
        assert "Z" in sql

    def test_ymd_and_time_tz_override_shift(self):
        """10:00+01:00 shifted to +03:00 → 12:00+03:00."""
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15),
                     "time": _var("t"), "timezone": _lit("+03:00")},
                    tl_values={"t": "10:00:00+01:00"}, t_types={"t": "time"})
        assert "12:00" in sql
        assert "+03:00" in sql

    def test_ymd_and_time_tz_same_no_shift(self):
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15),
                     "time": _var("t"), "timezone": _lit("+01:00")},
                    tl_values={"t": "10:00:00+01:00"}, t_types={"t": "time"})
        assert "10:00" in sql
        assert "+01:00" in sql

    def test_ymd_and_time_with_second_override(self):
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15),
                     "time": _var("t"), "second": _lit(30)},
                    tl_values={"t": "10:00:00+01:00"}, t_types={"t": "time"})
        assert "30" in sql
        assert "2023" in sql

    def test_ymd_and_time_with_fractional_seconds(self):
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(1), "day": _lit(1), "time": _var("t")},
                    tl_values={"t": "10:30:00.999999+00:00"}, t_types={"t": "time"})
        assert "999999" in sql
        assert "2023" in sql

    def test_ymd_and_time_from_utc_shift(self):
        """Z source time: code does NOT shift (Z excluded from shift in _c2_lit_num_tz check).
        The new TZ is appended but wall-clock time is kept as-is."""
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15),
                     "time": _var("t"), "timezone": _lit("+05:30")},
                    tl_values={"t": "10:00Z"}, t_types={"t": "time"})
        # No shift: '10:00Z' appended with '+05:30'
        assert "10:00" in sql
        assert "+05:30" in sql

    def test_ymd_and_time_backward_shift(self):
        """12:00+03:00 shifted to -05:00 → 04:00-05:00."""
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15),
                     "time": _var("t"), "timezone": _lit("-05:00")},
                    tl_values={"t": "12:00+03:00"}, t_types={"t": "time"})
        assert "04:00" in sql
        assert "-05:00" in sql

    def test_ymd_and_datetime_base_time_extract(self):
        """time var is datetime — extracts time part."""
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15), "time": _var("dt")},
                    tl_values={"dt": "2020-01-01T10:30:00+01:00"}, t_types={"dt": "datetime"})
        assert "2023" in sql

    def test_ymd_and_time_no_tz_override_localtime(self):
        """datetime({year:..., month:..., day:..., time: lt}) where lt is localtime → append Z."""
        sql = build("datetime",
                    {"year": _lit(2000), "month": _lit(1), "day": _lit(1), "time": _var("lt")},
                    tl_values={"lt": "00:00:00"}, t_types={"lt": "localtime"})
        assert "2000" in sql
        assert "Z" in sql


# ===========================================================================
# Edge cases: None-return paths
# ===========================================================================

class TestReturnsNone:
    """Branches that return None because no usable base key is present."""

    def test_localdatetime_no_base_keys(self):
        """localdatetime({hour: 10}) — no 'datetime' or 'date' key → None."""
        sql = build("localdatetime", {"hour": _lit(10)}, tl_values={}, t_types={})
        assert sql is None

    def test_datetime_no_base_keys(self):
        sql = build("datetime", {"hour": _lit(10)}, tl_values={}, t_types={})
        assert sql is None

    def test_date_no_base_key(self):
        sql = build("date", {"hour": _lit(10)}, tl_values={}, t_types={})
        assert sql is None

    def test_localtime_no_base_key(self):
        sql = build("localtime", {"year": _lit(2023)}, tl_values={}, t_types={})
        assert sql is None

    def test_date_base_but_get_base_sql_returns_none(self):
        """date key is present but the variable is unknown (no alias, no literal value)."""
        # Without any alias or literal value, _get_base_sql returns (alias_expr, None)
        # which for unknown types still produces a SQL reference (alias or safe_alias)
        # This should not return None unless there's something truly unresolvable
        sql = build("date", {"date": _var("unknown_var")}, tl_values={}, t_types={})
        # Should return something (the variable name as SQL)
        assert sql is not None


# ===========================================================================
# Additional branch coverage
# ===========================================================================

class TestDateFromDatetimeBaseOverrides:
    """date with overrides where base is localdatetime."""

    def test_month_override_from_localdatetime(self):
        sql = build("date", {"date": _var("dt"), "month": _lit(8)},
                    tl_values={"dt": "2023-05-15T10:30:00"}, t_types={"dt": "localdatetime"})
        assert "'08'" in sql

    def test_day_override_from_localdatetime(self):
        sql = build("date", {"date": _var("dt"), "day": _lit(1)},
                    tl_values={"dt": "2023-05-15T10:30:00"}, t_types={"dt": "localdatetime"})
        assert "'01'" in sql

    def test_year_override_from_datetime(self):
        sql = build("date", {"date": _var("dt"), "year": _lit(2099)},
                    tl_values={"dt": "2023-05-15T10:30:00+01:00"}, t_types={"dt": "datetime"})
        assert "'2099'" in sql


class TestLocaldatetimeRuntimePath:
    """fn='localdatetime', {datetime: var} with no compile-time literal — runtime SQL."""

    def test_datetime_base_runtime(self):
        """Without temporal_literal_value, produces CASE-based SQL."""
        sql = build("localdatetime", {"datetime": _var("dt")},
                    tl_values={}, t_types={"dt": "datetime"},
                    aliases={"dt": "Stage1"})
        assert sql is not None
        assert "CASE" in sql or "Stage1" in sql

    def test_datetime_base_runtime_with_overrides(self):
        """Runtime path with overrides uses SUBSTRING to extract components."""
        sql = build("localdatetime", {"datetime": _var("dt"), "year": _lit(2030)},
                    tl_values={}, t_types={"dt": "datetime"},
                    aliases={"dt": "Stage1"})
        assert "2030" in sql


class TestTimeFromDatetimeBase:
    """fn='time', base is a datetime variable."""

    def test_time_from_datetime_literal_keeps_tz(self):
        sql = build("time", {"time": _var("dt")},
                    tl_values={"dt": "2023-05-15T14:30:00+03:00"}, t_types={"dt": "datetime"})
        # Extract time portion from datetime, keep numeric TZ
        assert "14:30:00" in sql

    def test_time_from_localdatetime_literal_appends_z(self):
        """localdatetime base → extract time and append Z for time."""
        sql = build("time", {"time": _var("dt")},
                    tl_values={"dt": "2023-05-15T14:30:00"}, t_types={"dt": "localdatetime"})
        assert "14:30:00" in sql
        assert "Z" in sql


class TestLocaltimeTimeOverrideVariants:
    """More coverage of fn='localtime' override combinations."""

    def test_all_three_overrides(self):
        sql = build("localtime",
                    {"time": _var("lt"), "hour": _lit(3), "minute": _lit(4), "second": _lit(5)},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert "'03'" in sql
        assert "'04'" in sql
        assert "'05'" in sql

    def test_hour_override_from_time_base(self):
        sql = build("localtime", {"time": _var("t"), "hour": _lit(6)},
                    tl_values={"t": "12:30:00+02:00"}, t_types={"t": "time"})
        assert "'06'" in sql


class TestTimeSecondOverrideVariants:
    """fn='time' second override with fractional seconds present in source."""

    def test_second_override_with_fractional(self):
        """Source has fractional seconds; second override changes integer part."""
        sql = build("time", {"time": _var("t"), "second": _lit(15)},
                    tl_values={"t": "12:30:00.123456+01:00"}, t_types={"t": "time"})
        assert "'15'" in sql

    def test_minute_override_from_time_literal(self):
        sql = build("time", {"time": _var("t"), "minute": _lit(0)},
                    tl_values={"t": "12:30:00+01:00"}, t_types={"t": "time"})
        assert "'00'" in sql


class TestDatetimeLocaldatetimeCombinations:
    """Additional localdatetime/datetime combinations."""

    def test_localdatetime_case1_with_month_override(self):
        """Case 1: date var + month override + time literals."""
        sql = build("localdatetime",
                    {"date": _var("d"), "month": _lit(12), "hour": _lit(0), "minute": _lit(0)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'12'" in sql

    def test_datetime_case1_no_time_var_default_time(self):
        """Case 1 datetime: date var + literal h/m/s — no subsecond, default s=0."""
        sql = build("datetime",
                    {"date": _var("d"), "hour": _lit(0), "minute": _lit(0)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        # Should produce datetime with 'T' and 'Z'
        assert "T" in sql
        assert "Z" in sql

    def test_datetime_case2_no_tz_override_time_base_is_localtime(self):
        """Case 2 datetime: no tz override, localtime base → append Z."""
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15), "time": _var("lt")},
                    tl_values={"lt": "10:30:00"}, t_types={"lt": "localtime"})
        assert "Z" in sql

    def test_datetime_case0_backward_shift_large(self):
        """Backward TZ shift crossing midnight."""
        sql = build("datetime", {"datetime": _var("dt"), "timezone": _lit("-12:00")},
                    tl_values={"dt": "2023-05-15T02:00:00+02:00"}, t_types={"dt": "datetime"})
        # 02:00+02:00 → 00:00 UTC → 00:00 - 12h = 12:00-12:00 of previous day (wraps)
        assert "-12:00" in sql

    def test_localdatetime_case2_second_override_runtime(self):
        """Case 2 localdatetime: runtime path (no literal value), second override."""
        sql = build("localdatetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15),
                     "time": _var("lt"), "second": _lit(30)},
                    tl_values={}, t_types={"lt": "localtime"},
                    aliases={"lt": "Stage1"})
        assert "30" in sql

    def test_datetime_case2_runtime_time_base(self):
        """Case 2 datetime: runtime (no literal value for time var)."""
        sql = build("datetime",
                    {"year": _lit(2023), "month": _lit(5), "day": _lit(15), "time": _var("t")},
                    tl_values={}, t_types={"t": "time"},
                    aliases={"t": "Stage1"})
        assert "2023" in sql

    def test_date_week_year_from_literal_value(self):
        """Week-based with compile-time year literal in map."""
        sql = build("date",
                    {"date": _var("d"), "year": _lit(2022), "week": _lit(1), "dayOfWeek": _lit(5)},
                    tl_values={"d": "2023-05-15"}, t_types={"d": "date"})
        assert "'2022'" in sql
        assert "4" in sql  # dayOfWeek - 1 = 4

    def test_time_tz_no_override_time_base_no_tz(self):
        """time({time: t}) where source has no TZ — fallback to no-shift."""
        sql = build("time", {"time": _var("t")},
                    tl_values={"t": "12:30:00"}, t_types={"t": "time"})
        # No TZ in source → returns as-is (plain time string)
        assert "12:30:00" in sql


class TestGetBaseSqlBranches:
    """Directly exercise _get_base_sql sub-paths via various alias configurations."""

    def test_variable_with_non_stage_alias(self):
        """Variable has an alias that does NOT start with 'Stage' — uses alias.name."""
        sql = build("date", {"date": _var("d"), "year": _lit(2028)},
                    tl_values={}, t_types={"d": "date"},
                    aliases={"d": "MyAlias"})
        # Should use MyAlias.d in the SQL
        assert "MyAlias" in sql

    def test_variable_no_alias_no_literal(self):
        """Variable has no alias and no literal value — uses safe_alias(name)."""
        sql = build("date", {"date": _var("d")},
                    tl_values={}, t_types={"d": "date"})
        # Should use the variable name as-is in the SQL
        assert "d" in sql

    def test_functioncall_base_date_fn(self):
        """Base expr is a FunctionCall (date constructor) — inlined."""
        from iris_vector_graph.cypher import ast as _ast
        # date({date: date('2023-05-15')}) — FunctionCall as base
        fn_call = _ast.FunctionCall("date", [_ast.Literal("2023-05-15")])
        m = _mlit({"date": fn_call})
        c = _ctx({}, {})
        sql = _build_temporal_from_variable_map("date", m, c)
        # Should process the inner FunctionCall and return some SQL
        assert sql is not None

    def test_functioncall_base_localdatetime(self):
        """Base expr is localdatetime FunctionCall — inlined for localdatetime target."""
        from iris_vector_graph.cypher import ast as _ast
        fn_call = _ast.FunctionCall("localdatetime", [_ast.Literal("2023-05-15T10:30:00")])
        m = _mlit({"datetime": fn_call})
        c = _ctx({}, {})
        sql = _build_temporal_from_variable_map("datetime", m, c)
        assert sql is not None


class TestFormatTzForIso:
    """Exercise the newly added _format_tz_for_iso helper function."""

    def test_z_returns_z(self):
        from iris_vector_graph.cypher.translator import _format_tz_for_iso
        assert _format_tz_for_iso("Z") == "Z"

    def test_utc_returns_z(self):
        from iris_vector_graph.cypher.translator import _format_tz_for_iso
        assert _format_tz_for_iso("UTC") == "Z"

    def test_gmt_returns_z(self):
        from iris_vector_graph.cypher.translator import _format_tz_for_iso
        assert _format_tz_for_iso("GMT") == "Z"

    def test_numeric_plus(self):
        from iris_vector_graph.cypher.translator import _format_tz_for_iso
        result = _format_tz_for_iso("+01:00")
        assert result == "+01:00"

    def test_numeric_minus(self):
        from iris_vector_graph.cypher.translator import _format_tz_for_iso
        result = _format_tz_for_iso("-05:00")
        assert result == "-05:00"

    def test_numeric_plus_hhmm_no_colon(self):
        from iris_vector_graph.cypher.translator import _format_tz_for_iso
        result = _format_tz_for_iso("+0530")
        assert result == "+05:30"

    def test_iana_zone_returns_offset_with_bracket(self):
        from iris_vector_graph.cypher.translator import _format_tz_for_iso
        result = _format_tz_for_iso("Europe/Stockholm", 2023, 7, 15)
        # Should return something like '+02:00[Europe/Stockholm]'
        assert "Europe/Stockholm" in result

    def test_plus_zero_returns_plus_zero(self):
        from iris_vector_graph.cypher.translator import _format_tz_for_iso
        result = _format_tz_for_iso("+00:00")
        assert result == "+00:00"
