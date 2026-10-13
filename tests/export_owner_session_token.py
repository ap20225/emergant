"""Check test-session availability without exporting secret values."""

from session_utils import get_owner_session_token


def main():
    get_owner_session_token()
    print("Existing owner test session available; no secret exported")


if __name__ == "__main__":
    main()
