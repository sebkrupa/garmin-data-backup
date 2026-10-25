"""Every Garmin Personal API data set the backup knows about.

Each Dataset maps one API endpoint to one SQLite table. Placeholders in
``path`` and ``params`` values: {start}, {end} (inclusive range), {date}
(one day) and {today}.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

Rows = Callable[[Any], list]
Key = Callable[[dict, str], str]


def _as_list(response: Any) -> list:
    if response is None:
        return []
    return response if isinstance(response, list) else [response]


def _calendar_date(item: dict, requested: str) -> str:
    return str(item.get("calendarDate") or requested)


@dataclass(frozen=True)
class Dataset:
    name: str
    path: str
    # range: date range per call; day: one call per day; once: every run.
    kind: str
    # Swagger schema of one stored row; it gives the typed columns.
    schema: str | None
    max_days: int = 1
    params: dict[str, str] = field(default_factory=dict)
    rows: Rows = _as_list
    key: Key = _calendar_date
    cost: str = ""
    # False: the generated datasets.txt lists it commented out.
    default_on: bool = True
    # True: not one row per calendar day, so absent days are not gaps.
    sparse: bool = False


# shortcut: Swagger gives no max range for hrv/goals/wellness activity, so
# 28 days is used; raise it if Garmin accepts more.
DATASETS: list[Dataset] = [
    # --- date ranges: cheap ---
    Dataset("daily_summary", "/usersummary-service/v2/dailysummaries", "range",
            "UserDailySummaryV2DTO", 365, {"startDate": "{start}", "endDate": "{end}"},
            rows=lambda r: (r or {}).get("data") or []),
    Dataset("steps_daily", "/usersummary-service/stats/steps/daily/{start}/{end}", "range",
            "StepsDailyStatDTO", 28),
    Dataset("stress_daily", "/usersummary-service/stats/stress/daily/{start}/{end}", "range",
            "MultiValueWellnessChartItemDTO", 28),
    Dataset("hydration_daily", "/usersummary-service/stats/hydration/daily/{start}/{end}", "range",
            "UserDailyHydrationDTO", 31),
    Dataset("sleep_daily", "/sleep-service/sleep/dailySleepSummariesByDate", "range",
            "DailySleepDTO", 31, {"startDate": "{start}", "endDate": "{end}"}),
    Dataset("body_battery_daily", "/wellness-service/wellness/bodyBattery/reports/daily", "range",
            "BodyBatteryDailyReportDTO", 31, {"startDate": "{start}", "endDate": "{end}"},
            key=lambda item, d: str(item.get("date") or d)),
    Dataset("hrv_summary", "/hrv-service/hrv/daily/{start}/{end}", "range",
            "HrvSummaryDTO", 28, rows=lambda r: (r or {}).get("hrvSummaries") or []),
    Dataset("weight_daily", "/weight-service/weight/range/{start}/{end}", "range",
            "DailyWeightSummary", 365, {"includeAll": "true"},
            rows=lambda r: (r or {}).get("dailyWeightSummaries") or [],
            key=lambda item, d: str(item.get("summaryDate") or d), sparse=True),
    # /weight-service/weight/all is skipped: it returns HTTP 400 for every range;
    # weight_daily.allWeightMetrics already holds every entry.
    Dataset("weight_goals", "/goal-service/goal/user/effective/weightgoal/{start}/{end}", "range",
            "UserGoalsRangeDTO", 28, key=lambda item, d: d, sparse=True),
    Dataset("wellness_activity", "/wellnessactivity-service/activity/summary/range/{start}/{end}",
            "range", "WellnessActivitySummaryDTO", 28,
            key=lambda item, d: json.dumps(item.get("activityUuid"), sort_keys=True), sparse=True),
    # --- one call per day: expensive (~365 calls per year each) ---
    Dataset("hrv_detail", "/hrv-service/hrv/{date}", "day", "HrvChartDTO",
            cost="~365 calls/year"),
    Dataset("heart_rate", "/wellness-service/wellness/dailyHeartRate", "day",
            "DailyHeartRateDTO", params={"date": "{date}"}, key=lambda item, d: d,
            cost="~365 calls/year"),
    Dataset("spo2", "/wellness-service/wellness/daily/spo2acclimation/{date}", "day",
            "DailySpO2AndAcclimationDTO", key=lambda item, d: d, cost="~365 calls/year"),
    Dataset("food_logs", "/nutrition-service/food/logs/{date}", "day",
            "DailyNutritionSummaryDTO", key=lambda item, d: d, cost="~365 calls/year",
            default_on=False),
    Dataset("menstrual_summary", "/periodichealth-service/menstrualcycle/summary/{date}", "day",
            "MenstrualCycleSummaryDTO", key=lambda item, d: d, cost="~365 calls/year",
            default_on=False),
    Dataset("menstrual_dayview", "/periodichealth-service/menstrualcycle/dayview/{date}", "day",
            "DetailedMenstrualCycleDayDTO", params={"todayDate": "{today}"},
            key=lambda item, d: d, cost="~365 calls/year",
            default_on=False),
    # --- every run: current state ---
    Dataset("profile_settings", "/userprofile-service/userprofile/settings", "once",
            "UserSettingsDTO", key=lambda item, d: "current"),
    Dataset("heart_rate_zones", "/biometric-service/heartRateZones/v2", "once",
            "HeartRateZonesDTO",
            key=lambda item, d: f"{item.get('trainingMethod')}|{item.get('sport')}"),
    Dataset("fitness_age", "/fitnessage-service/fitnessage/v2/{today}", "once", "FitnessAgeDTO",
            key=lambda item, d: f"{d}|{item.get('lastUpdated')}"),
    # --- activities: search list + detail + workouts + original file per activity ---
    Dataset("activities", "/activitylist-service/activities/search/activities", "activities",
            "SummarizedActivityDTO", 365, cost="3 calls per activity"),
]

BY_NAME = {d.name: d for d in DATASETS}

# Extra tables filled by the "activities" dataset.
ACTIVITY_DETAIL = Dataset("activity_detail", "/activity-service/activity/{id}", "activities",
                          "ActivitySummaryDTO")
ACTIVITY_WORKOUTS = Dataset("activity_workouts", "/activity-service/activity/{id}/workouts",
                            "activities", None)
