"""참가자 계정을 만들고 확인하는 CLI.

    python manage_accounts.py list
    python manage_accounts.py add P-101 P-102 P-103
    python manage_accounts.py add P-104 --password 직접-정한-비밀번호
    python manage_accounts.py add P-105 --credit-limit 20
    python manage_accounts.py add --random-count 10

비밀번호를 주지 않으면 읽기 쉬운 임시 비밀번호를 만들어 출력한다. 그 출력이
참가자에게 전달할 값이고, 해시만 저장되므로 **다시 볼 수 없다.** 터미널 기록을
남기거나 바로 안내문에 옮겨 두어야 한다.

`DATA_DIR`과 `DATABASE_PATH`는 서버와 같은 `server/.env`에서 읽으므로, 배포된
VM에서 그대로 실행하면 서버가 쓰는 DB를 가리킨다.
"""

from __future__ import annotations

import argparse
import secrets
import string

from persistence.sqlite import SqliteStore
from runtime.config import load_settings
from runtime.services import open_store as configured_store

# 사람이 받아 적고 다시 입력해야 하므로 서로 헷갈리는 글자(0/O, 1/l/I)는 뺀다.
_ALPHABET = "".join(
    ch for ch in string.ascii_lowercase + string.digits if ch not in "01ol"
)


def generate_password(groups: int = 3, size: int = 4) -> str:
    return "-".join(
        "".join(secrets.choice(_ALPHABET) for _ in range(size)) for _ in range(groups)
    )


def generate_study_id() -> str:
    """Return a validator-compatible login ID with 128 bits of randomness."""
    return f"P-{secrets.token_urlsafe(16)}"


def open_store(default_credit_limit: float) -> SqliteStore:
    return configured_store(load_settings({"DEFAULT_CREDIT_LIMIT_USD": default_credit_limit}))


def command_add(args: argparse.Namespace) -> None:
    if args.random_count is not None and args.study_ids:
        raise SystemExit("STUDY_ID와 --random-count는 함께 쓸 수 없다")
    if args.random_count is not None and args.random_count < 1:
        raise SystemExit("--random-count는 1 이상이어야 한다")
    study_ids = (
        [generate_study_id() for _ in range(args.random_count)]
        if args.random_count is not None
        else args.study_ids
    )
    if not study_ids:
        raise SystemExit("STUDY_ID를 주거나 --random-count를 사용해야 한다")
    if args.password and len(study_ids) > 1:
        raise SystemExit("--password는 참가자 한 명에게만 쓸 수 있다")
    store = open_store(args.credit_limit)
    print(f"{'참가자':>8}  {'비밀번호':<16}  상태")
    for study_id in study_ids:
        study_id = SqliteStore.validate_study_id(study_id)
        password = args.password or generate_password()
        before = store.authenticate(study_id, password)
        store.ensure_account(study_id, password, args.credit_limit)
        # ensure_account는 이미 있는 계정을 건드리지 않는다. 방금 준 비밀번호로
        # 인증되지 않으면 그 계정은 원래 있던 것이고 비밀번호도 그대로다.
        created = store.authenticate(study_id, password) and not before
        state = "생성됨" if created else "이미 있음 (비밀번호 그대로)"
        shown = password if created else "—"
        print(f"{study_id:>8}  {shown:<16}  {state}")
    print("\n비밀번호는 해시로만 저장된다. 위 출력을 지금 옮겨 두지 않으면 다시 볼 수 없다.")


def command_list(args: argparse.Namespace) -> None:
    store = open_store(args.credit_limit)
    rows = store.list_accounts()
    if not rows:
        print("계정이 없다.")
        return
    print(f"{'참가자':>8}  {'활성':>4}  {'한도':>8}  {'대화':>5}  {'메시지':>6}")
    for row in rows:
        print(
            f"{row['study_id']:>8}  {'예' if row['active'] else '아니오':>4}"
            f"  ${row['credit_limit_usd']:>7.2f}  {row['conversations']:>5}  {row['messages']:>6}"
        )


def main() -> None:
    settings = load_settings()
    # --credit-limit은 서브커맨드 뒤에 쓰는 게 자연스러우므로 공용 부모로 둔다.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--credit-limit",
        type=float,
        default=settings["DEFAULT_CREDIT_LIMIT_USD"],
        help="참가자 1인당 모델 호출 비용 상한 (기본: .env의 DEFAULT_CREDIT_LIMIT_USD)",
    )

    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="참가자 계정을 만든다", parents=[common])
    add.add_argument("study_ids", nargs="*", metavar="STUDY_ID")
    add.add_argument(
        "--random-count",
        type=int,
        metavar="COUNT",
        help="128비트 랜덤 로그인 ID를 COUNT개 자동 생성",
    )
    add.add_argument("--password", help="직접 정한 비밀번호 (생략하면 자동 생성)")
    add.set_defaults(handler=command_add)

    listing = subparsers.add_parser("list", help="계정과 기록량을 확인한다", parents=[common])
    listing.set_defaults(handler=command_list)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
