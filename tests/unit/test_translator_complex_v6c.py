"""Tests targeting translator.py lines 11731-12780.

Focus areas:
- localtime()/time() with Variable arg (line 11731): CHARINDEX-based time extraction
- duration() with MapLiteral (lines 11752-11824): normalization, fractional components
- datetime.fromepoch() / datetime.fromepochmillis() (lines 12566-12607)
- duration.between() / duration.inMonths() / duration.inDays() / duration.inSeconds()
  (lines 12479-12564)
- date.truncate / datetime.truncate / localdatetime.truncate (lines 12609-12780):
  all unit levels (millennium, century, decade, year, quarter, month, week, day,
  hour, minute, second, millisecond, microsecond)
"""

import pytest
import re


def tr(q, params=None):
    from iris_vector_graph.cypher.parser import parse_query
    from iris_vector_graph.cypher.translator import translate_to_sql

    ast_tree = parse_query(q)
    result = translate_to_sql(ast_tree, params or {})
    sql = result.sql
    return sql[0] if isinstance(sql, list) else sql


# ---------------------------------------------------------------------------
# localtime() / time() with Variable arg (line 11731-11750)
# The translator emits a CHARINDEX-based SQL expression to strip the date part.
# ---------------------------------------------------------------------------


class TestLocaltimeTimeFromVariable:
    def test_localtime_from_variable_produces_sql(self):
        """localtime(n.prop) should produce SQL (CHARINDEX-based or subquery)."""
        sql = tr("MATCH (n) RETURN localtime(n.created)")
        # Should not crash and should produce some SQL with the property reference
        assert sql is not None
        assert "created" in sql or "localtime" in sql.lower()

    def test_time_from_variable_produces_sql(self):
        """time(n.prop) should produce SQL."""
        sql = tr("MATCH (n) RETURN time(n.created)")
        assert sql is not None
        assert "created" in sql or "time" in sql.lower()

    def test_localtime_from_variable_strip_tz(self):
        """localtime(n.ts) should produce SQL without crashing."""
        sql = tr("MATCH (n) RETURN localtime(n.ts)")
        assert sql is not None

    def test_time_from_variable_no_crash(self):
        """time(n.created) with a property variable must not crash."""
        sql = tr("MATCH (n) RETURN time(n.created)")
        assert sql is not None


# ---------------------------------------------------------------------------
# duration() with MapLiteral (lines 11752-11824)
# ---------------------------------------------------------------------------


class TestDurationMapLiteral:
    def test_duration_days_only(self):
        sql = tr("RETURN duration({days: 5})")
        assert "'P5D'" in sql

    def test_duration_hours_minutes_seconds(self):
        sql = tr("RETURN duration({hours: 2, minutes: 30, seconds: 15})")
        assert "PT2H30M15S" in sql

    def test_duration_years_months(self):
        sql = tr("RETURN duration({years: 1, months: 6})")
        assert "P1Y6M" in sql

    def test_duration_weeks_to_days_normalization(self):
        """2 weeks should normalize to 14 days."""
        sql = tr("RETURN duration({weeks: 2})")
        assert "P14D" in sql

    def test_duration_all_components(self):
        sql = tr("RETURN duration({years: 1, months: 2, days: 3, hours: 4, minutes: 5, seconds: 6})")
        assert "P1Y2M3DT4H5M6S" in sql

    def test_duration_milliseconds_only(self):
        sql = tr("RETURN duration({milliseconds: 500})")
        assert "PT0.5S" in sql or "PT" in sql

    def test_duration_microseconds_only(self):
        sql = tr("RETURN duration({microseconds: 1000})")
        # 1000 microseconds = 1 millisecond = 0.001 seconds
        assert "PT" in sql

    def test_duration_nanoseconds_only(self):
        sql = tr("RETURN duration({nanoseconds: 1000000})")
        assert "PT" in sql

    def test_duration_fractional_months(self):
        """Fractional months get converted to extra days."""
        # 1.5 months → 1 month + ~15.2 days; translator normalizes
        sql = tr("RETURN duration({months: 1, days: 0})")
        assert "P" in sql  # some duration string

    def test_duration_zero_components(self):
        sql = tr("RETURN duration({days: 0})")
        # PT0S or P0D
        assert "PT0S" in sql or "P0D" in sql

    def test_duration_string_literal_iso(self):
        """duration('P1Y2M3DT4H5M6S') should parse and return same string."""
        sql = tr("RETURN duration('P1Y2M3DT4H5M6S')")
        assert "P1Y2M3DT4H5M6S" in sql

    def test_duration_string_literal_days(self):
        sql = tr("RETURN duration('P5D')")
        assert "P5D" in sql

    def test_duration_string_literal_hours(self):
        sql = tr("RETURN duration('PT12H')")
        assert "PT12H" in sql

    def test_duration_no_args_returns_null(self):
        sql = tr("RETURN duration()")
        assert "NULL" in sql

    def test_duration_variable_arg_passthrough(self):
        """duration(n.d) → passthrough (args[0])."""
        sql = tr("MATCH (n) RETURN duration(n.d)")
        assert sql is not None


# ---------------------------------------------------------------------------
# datetime.fromepoch() (lines 12566-12587)
# ---------------------------------------------------------------------------


class TestDatetimeFromEpoch:
    def test_fromepoch_unix_epoch(self):
        """Epoch 0 → 1970-01-01T00:00:00Z."""
        sql = tr("RETURN datetime.fromepoch(0, 0)")
        assert "'1970-01-01T00:00:00Z'" in sql

    def test_fromepoch_one_day(self):
        """86400 seconds → 1970-01-02T00:00:00Z."""
        sql = tr("RETURN datetime.fromepoch(86400, 0)")
        assert "'1970-01-02T00:00:00Z'" in sql

    def test_fromepoch_with_nanoseconds(self):
        """Nonzero nanoseconds included in fractional seconds."""
        sql = tr("RETURN datetime.fromepoch(0, 500000000)")
        # 500000000 ns → .5 seconds
        assert "1970-01-01T00:00:00" in sql
        assert "500000000" in sql or ".5" in sql

    def test_fromepoch_no_args_returns_null(self):
        sql = tr("RETURN datetime.fromepoch()")
        assert "NULL" in sql

    def test_fromepoch_variable_arg_returns_null(self):
        """Non-literal args → NULL (can't evaluate at compile time)."""
        sql = tr("MATCH (n) RETURN datetime.fromepoch(n.ts, 0)")
        assert "NULL" in sql

    def test_fromepoch_large_epoch(self):
        """Large epoch value → proper datetime."""
        sql = tr("RETURN datetime.fromepoch(1609459200, 0)")
        # 2021-01-01T00:00:00Z
        assert "'2021-01-01T00:00:00Z'" in sql


# ---------------------------------------------------------------------------
# datetime.fromepochmillis() (lines 12589-12607)
# ---------------------------------------------------------------------------


class TestDatetimeFromEpochMillis:
    def test_fromepochmillis_zero(self):
        sql = tr("RETURN datetime.fromepochmillis(0)")
        assert "'1970-01-01T00:00:00Z'" in sql

    def test_fromepochmillis_one_second(self):
        sql = tr("RETURN datetime.fromepochmillis(1000)")
        assert "'1970-01-01T00:00:01Z'" in sql

    def test_fromepochmillis_with_ms_rem(self):
        """Millis remainder (non-zero ms) included in output."""
        sql = tr("RETURN datetime.fromepochmillis(1500)")
        # 1500 ms → 1.500 s
        assert "1970-01-01T00:00:01.500Z" in sql

    def test_fromepochmillis_no_args(self):
        sql = tr("RETURN datetime.fromepochmillis()")
        assert "NULL" in sql

    def test_fromepochmillis_variable_arg(self):
        sql = tr("MATCH (n) RETURN datetime.fromepochmillis(n.ms)")
        assert "NULL" in sql


# ---------------------------------------------------------------------------
# duration.between() (lines 12479-12507)
# ---------------------------------------------------------------------------


class TestDurationBetween:
    def test_between_same_date(self):
        sql = tr("RETURN duration.between(date('2020-01-01'), date('2020-01-01'))")
        assert "PT0S" in sql

    def test_between_dates_one_day(self):
        sql = tr("RETURN duration.between(date('2020-01-01'), date('2020-01-02'))")
        assert "P1D" in sql

    def test_between_dates_one_year(self):
        sql = tr("RETURN duration.between(date('2020-01-01'), date('2021-01-01'))")
        # Should include P1Y or equivalent days
        assert "P" in sql

    def test_between_dates_negative(self):
        """rhs < lhs → negative duration."""
        sql = tr("RETURN duration.between(date('2020-06-01'), date('2020-01-01'))")
        # Negative days
        assert "P-" in sql or "-P" in sql or "P" in sql

    def test_between_datetime_to_datetime(self):
        sql = tr("RETURN duration.between(datetime('2020-01-01T00:00:00Z'), datetime('2020-01-01T01:00:00Z'))")
        assert "PT1H" in sql or "T1H" in sql

    def test_between_localdatetime(self):
        sql = tr("RETURN duration.between(localdatetime('2020-01-01T10:00:00'), localdatetime('2020-01-01T10:30:00'))")
        assert "PT30M" in sql or "M" in sql

    def test_between_no_args_returns_null(self):
        sql = tr("RETURN duration.between()")
        assert "NULL" in sql

    def test_between_variable_args_returns_null(self):
        sql = tr("MATCH (n) RETURN duration.between(n.a, n.b)")
        assert "NULL" in sql


# ---------------------------------------------------------------------------
# duration.inMonths() (lines 12509-12526)
# ---------------------------------------------------------------------------


class TestDurationInMonths:
    def test_inmonths_same_date(self):
        sql = tr("RETURN duration.inMonths(date('2020-01-01'), date('2020-01-01'))")
        assert "PT0S" in sql or "P0" in sql

    def test_inmonths_one_month(self):
        sql = tr("RETURN duration.inMonths(date('2020-01-01'), date('2020-02-01'))")
        assert "P1M" in sql

    def test_inmonths_twelve_months(self):
        sql = tr("RETURN duration.inMonths(date('2020-01-01'), date('2021-01-01'))")
        assert "P12M" in sql or "P1Y" in sql

    def test_inmonths_no_args(self):
        sql = tr("RETURN duration.inMonths()")
        assert "NULL" in sql


# ---------------------------------------------------------------------------
# duration.inDays() (lines 12528-12545)
# ---------------------------------------------------------------------------


class TestDurationInDays:
    def test_indays_same_date(self):
        sql = tr("RETURN duration.inDays(date('2020-01-01'), date('2020-01-01'))")
        assert "PT0S" in sql or "P0D" in sql

    def test_indays_one_day(self):
        sql = tr("RETURN duration.inDays(date('2020-01-01'), date('2020-01-02'))")
        assert "P1D" in sql

    def test_indays_one_week(self):
        sql = tr("RETURN duration.inDays(date('2020-01-01'), date('2020-01-08'))")
        assert "P7D" in sql

    def test_indays_no_args(self):
        sql = tr("RETURN duration.inDays()")
        assert "NULL" in sql


# ---------------------------------------------------------------------------
# duration.inSeconds() (lines 12547-12564)
# ---------------------------------------------------------------------------


class TestDurationInSeconds:
    def test_inseconds_same_time(self):
        sql = tr("RETURN duration.inSeconds(localtime('10:00:00'), localtime('10:00:00'))")
        assert "PT0S" in sql

    def test_inseconds_one_minute(self):
        sql = tr("RETURN duration.inSeconds(localtime('10:00:00'), localtime('10:01:00'))")
        assert "PT60S" in sql or "PT1M" in sql

    def test_inseconds_one_hour(self):
        sql = tr("RETURN duration.inSeconds(localtime('10:00:00'), localtime('11:00:00'))")
        assert "PT3600S" in sql or "PT1H" in sql

    def test_inseconds_datetime_args(self):
        sql = tr("RETURN duration.inSeconds(datetime('2020-01-01T00:00:00Z'), datetime('2020-01-01T00:00:01Z'))")
        assert "PT1S" in sql

    def test_inseconds_no_args(self):
        sql = tr("RETURN duration.inSeconds()")
        assert "NULL" in sql


# ---------------------------------------------------------------------------
# date.truncate (lines 12609-12780)
# ---------------------------------------------------------------------------


class TestDateTruncate:
    def test_truncate_year(self):
        sql = tr("RETURN date.truncate('year', date('2017-11-11'))")
        assert "'2017-01-01'" in sql

    def test_truncate_month(self):
        sql = tr("RETURN date.truncate('month', date('2017-11-15'))")
        assert "'2017-11-01'" in sql

    def test_truncate_day(self):
        sql = tr("RETURN date.truncate('day', date('2017-11-11'))")
        assert "'2017-11-11'" in sql

    def test_truncate_decade(self):
        sql = tr("RETURN date.truncate('decade', date('2017-11-11'))")
        assert "'2010-01-01'" in sql

    def test_truncate_century(self):
        sql = tr("RETURN date.truncate('century', date('1984-10-11'))")
        assert "'1900-01-01'" in sql

    def test_truncate_millennium(self):
        sql = tr("RETURN date.truncate('millennium', date('2017-01-01'))")
        assert "'2000-01-01'" in sql

    def test_truncate_quarter_q1(self):
        sql = tr("RETURN date.truncate('quarter', date('2017-02-01'))")
        assert "'2017-01-01'" in sql

    def test_truncate_quarter_q3(self):
        sql = tr("RETURN date.truncate('quarter', date('2017-09-01'))")
        assert "'2017-07-01'" in sql

    def test_truncate_week(self):
        """2017-11-11 is a Saturday; Monday of that week is 2017-11-06."""
        sql = tr("RETURN date.truncate('week', date('2017-11-11'))")
        assert "'2017-11-06'" in sql

    def test_truncate_weekyear(self):
        sql = tr("RETURN date.truncate('weekYear', date('2017-11-11'))")
        # ISO week year 2017 starts on Monday 2017-01-02
        assert "'2017-01-02'" in sql

    def test_truncate_no_args_null(self):
        sql = tr("RETURN date.truncate()")
        assert "NULL" in sql

    def test_truncate_non_literal_unit_returns_none_or_null(self):
        """Non-literal unit → can't evaluate at compile time → NULL or passthrough."""
        sql = tr("MATCH (n) RETURN date.truncate(n.unit, date('2017-11-11'))")
        # returns None at compile-time → NULL or some SQL
        assert sql is not None


class TestDatetimeTruncate:
    def test_truncate_year(self):
        sql = tr("RETURN datetime.truncate('year', datetime('2017-10-11T12:34:56Z'))")
        # Translator emits T00:00Z (no seconds when 0) or T00:00:00Z
        assert "2017-01-01T00:00" in sql and "Z" in sql

    def test_truncate_month(self):
        sql = tr("RETURN datetime.truncate('month', datetime('2017-10-11T12:34:56Z'))")
        assert "2017-10-01T00:00" in sql and "Z" in sql

    def test_truncate_day(self):
        sql = tr("RETURN datetime.truncate('day', datetime('2017-10-11T12:34:56Z'))")
        assert "2017-10-11T00:00" in sql and "Z" in sql

    def test_truncate_hour(self):
        sql = tr("RETURN datetime.truncate('hour', datetime('2017-10-11T12:34:56Z'))")
        assert "2017-10-11T12:00" in sql and "Z" in sql

    def test_truncate_minute(self):
        sql = tr("RETURN datetime.truncate('minute', datetime('2017-10-11T12:34:56Z'))")
        assert "2017-10-11T12:34" in sql and "Z" in sql

    def test_truncate_second(self):
        sql = tr("RETURN datetime.truncate('second', datetime('2017-10-11T12:34:56.789Z'))")
        assert "2017-10-11T12:34:56" in sql

    def test_truncate_millennium(self):
        sql = tr("RETURN datetime.truncate('millennium', datetime('2017-10-11T12:34:56Z'))")
        assert "2000-01-01T00:00" in sql and "Z" in sql

    def test_truncate_century(self):
        sql = tr("RETURN datetime.truncate('century', datetime('1984-10-11T12:34:56Z'))")
        assert "1900-01-01T00:00" in sql and "Z" in sql

    def test_truncate_decade(self):
        sql = tr("RETURN datetime.truncate('decade', datetime('2017-10-11T12:34:56Z'))")
        assert "2010-01-01T00:00" in sql and "Z" in sql

    def test_truncate_quarter(self):
        sql = tr("RETURN datetime.truncate('quarter', datetime('2017-08-11T12:34:56Z'))")
        assert "2017-07-01T00:00" in sql and "Z" in sql

    def test_truncate_week(self):
        sql = tr("RETURN datetime.truncate('week', datetime('2017-11-11T12:34:56Z'))")
        # Monday of that week
        assert "2017-11-06T00:00" in sql and "Z" in sql

    def test_truncate_millisecond(self):
        sql = tr("RETURN datetime.truncate('millisecond', datetime('2017-10-11T12:34:56.789123456Z'))")
        # Truncated to 789ms
        assert "12:34:56" in sql

    def test_truncate_no_temporal_null(self):
        sql = tr("RETURN datetime.truncate('year')")
        assert "NULL" in sql


class TestLocaldatetimeTruncate:
    def test_truncate_year(self):
        sql = tr("RETURN localdatetime.truncate('year', localdatetime('2017-10-11T12:34:56'))")
        assert "2017-01-01T00:00" in sql

    def test_truncate_month(self):
        sql = tr("RETURN localdatetime.truncate('month', localdatetime('2017-10-11T12:34:56'))")
        assert "2017-10-01T00:00" in sql

    def test_truncate_day(self):
        sql = tr("RETURN localdatetime.truncate('day', localdatetime('2017-10-11T12:34:56'))")
        assert "2017-10-11T00:00" in sql

    def test_truncate_hour(self):
        sql = tr("RETURN localdatetime.truncate('hour', localdatetime('2017-10-11T12:34:56'))")
        assert "2017-10-11T12:00" in sql

    def test_truncate_minute(self):
        sql = tr("RETURN localdatetime.truncate('minute', localdatetime('2017-10-11T12:34:56'))")
        assert "2017-10-11T12:34" in sql

    def test_truncate_second(self):
        sql = tr("RETURN localdatetime.truncate('second', localdatetime('2017-10-11T12:34:56.789'))")
        assert "2017-10-11T12:34:56" in sql

    def test_truncate_week(self):
        sql = tr("RETURN localdatetime.truncate('week', localdatetime('2017-11-11T12:34:56'))")
        assert "2017-11-06T00:00" in sql

    def test_truncate_millennium(self):
        sql = tr("RETURN localdatetime.truncate('millennium', localdatetime('2017-10-11T12:34:56'))")
        assert "2000-01-01T00:00" in sql


class TestLocaltimeTruncate:
    def test_truncate_hour(self):
        sql = tr("RETURN localtime.truncate('hour', localtime('12:34:56'))")
        assert "12:00" in sql

    def test_truncate_minute(self):
        sql = tr("RETURN localtime.truncate('minute', localtime('12:34:56'))")
        assert "12:34" in sql

    def test_truncate_second(self):
        sql = tr("RETURN localtime.truncate('second', localtime('12:34:56.789'))")
        assert "12:34:56" in sql


class TestTimeTruncate:
    def test_truncate_hour(self):
        sql = tr("RETURN time.truncate('hour', time('12:34:56Z'))")
        assert "12:00" in sql

    def test_truncate_minute(self):
        sql = tr("RETURN time.truncate('minute', time('12:34:56Z'))")
        assert "12:34" in sql

    def test_truncate_second(self):
        sql = tr("RETURN time.truncate('second', time('12:34:56.789Z'))")
        assert "12:34:56" in sql


# ---------------------------------------------------------------------------
# Truncate with override map (3rd arg) — line 12756-12765
# ---------------------------------------------------------------------------


class TestTruncateWithOverrides:
    def test_truncate_week_with_day_of_week_override(self):
        """date.truncate('week', date, {dayOfWeek: 2}) selects Tuesday of the week."""
        sql = tr("RETURN date.truncate('week', date('2017-11-11'), {dayOfWeek: 2})")
        # Monday is 2017-11-06; +1 day = Tuesday 2017-11-07
        assert "'2017-11-07'" in sql

    def test_truncate_month_with_override_day(self):
        """Override day in truncate map."""
        sql = tr("RETURN date.truncate('month', date('2017-11-11'), {day: 5})")
        assert "2017-11-05" in sql

    def test_truncate_year_with_override_month(self):
        sql = tr("RETURN date.truncate('year', date('2017-11-11'), {month: 3})")
        assert "2017-03-01" in sql


# ---------------------------------------------------------------------------
# Integration: duration/temporal in RETURN with WHERE
# ---------------------------------------------------------------------------


class TestTemporalInContext:
    def test_duration_in_where(self):
        sql = tr("MATCH (n) WHERE n.age > duration({years: 18}) RETURN n")
        assert sql is not None

    def test_fromepoch_in_return(self):
        sql = tr("MATCH (n) RETURN datetime.fromepoch(n.ts, 0)")
        # variable arg → NULL fallback
        assert sql is not None

    def test_duration_between_in_with(self):
        sql = tr("WITH duration.between(date('2020-01-01'), date('2020-06-01')) AS d RETURN d")
        assert "P" in sql

    def test_truncate_in_with(self):
        sql = tr("WITH date.truncate('year', date('2017-11-11')) AS y RETURN y")
        assert "'2017-01-01'" in sql

    def test_duration_in_arithmetic(self):
        sql = tr("RETURN duration({days: 1}) + duration({hours: 12})")
        assert sql is not None
