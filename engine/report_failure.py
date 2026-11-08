import os
import re
import sys

from record import notify_dashboard_status, refresh_tracked_user


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[0-9a-f]{64}", sys.argv[1]):
        raise ValueError("Usage: report_failure.py HOST_KEY")

    required = (
        "GITHUB_REPOSITORY",
        "GITHUB_TOKEN",
        "HOST_KEY_SECRET",
        "DASHBOARD_API_URL",
        "DASHBOARD_API_TOKEN",
    )
    if any(not os.environ.get(key) for key in required):
        print("::warning::Recorder failure could not be reported; integration configuration is incomplete")
        return

    try:
        username = refresh_tracked_user(
            sys.argv[1],
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GITHUB_TOKEN"],
            os.environ["HOST_KEY_SECRET"],
        )
        if not username:
            print("::warning::Recorder failed for an account that is no longer tracked")
            return
        notify_dashboard_status(
            username,
            None,
            "error",
            (
                "Recorder failed: "
                + os.environ.get("LIVEVAULT_FAILURE_REASON", "see GitHub Actions run")
            )[:200],
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"::warning::Recorder failure could not be reported ({type(exc).__name__})")
        return
    print("RESULT: recorder failure reported to dashboard")


if __name__ == "__main__":
    main()
