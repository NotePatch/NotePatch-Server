import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from notepatch.platform.database import SessionLocal
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService


def main() -> None:
    service = OpenClawUserRuntimeService()
    with SessionLocal() as db:
        users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.created_at.asc())).all()
        for user in users:
            workspace = db.scalar(select(Workspace).where(Workspace.owner_user_id == user.id))
            if workspace is None:
                print(f"skip user={user.id} email={user.email}: no personal workspace")
                continue
            runtime = service.provision_user(user, workspace)
            print(
                "provisioned "
                f"user={user.id} email={user.email} "
                f"container={runtime['container_name']} "
                f"root={runtime['user_root']}"
            )


if __name__ == "__main__":
    main()
