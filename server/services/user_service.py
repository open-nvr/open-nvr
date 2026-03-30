# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
User service for business logic operations.
Handles user creation, updates, authentication, and management.
"""

from fastapi import HTTPException, status
from sqlalchemy import and_
from sqlalchemy.orm import Session

from core.auth import get_password_hash, verify_password
from models import PasswordPolicy, Role, User
from schemas import UserCreate, UserUpdate


class UserService:
    """Service class for user-related operations."""

    @staticmethod
    def create_user(db: Session, user_create: UserCreate) -> User:
        """Create a new user."""
        if db.query(User).filter(User.username == user_create.username).first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already registered",
            )
        if db.query(User).filter(User.email == user_create.email).first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )
        role = db.query(Role).filter(Role.id == user_create.role_id).first()
        if not role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Role not found"
            )

        try:
            with db.begin_nested():
                # Enforce password policy
                UserService._enforce_password_policy(
                    db, user_create.username, user_create.email, user_create.password
                )

                hashed_password = get_password_hash(user_create.password)
                db_user = User(
                    username=user_create.username,
                    email=user_create.email,
                    hashed_password=hashed_password,
                    first_name=user_create.first_name,
                    last_name=user_create.last_name,
                    is_active=user_create.is_active,
                    password_set=True,  # Normal user creation has password set
                    role_id=user_create.role_id,
                )
                db.add(db_user)
                db.flush()

            db.commit()
            db.refresh(db_user)
            return db_user
        except Exception as e:
            db.rollback()
            raise e

    @staticmethod
    def get_user_by_id(db: Session, user_id: int) -> User | None:
        return db.query(User).filter(User.id == user_id).first()

    @staticmethod
    def get_user_by_username(db: Session, username: str) -> User | None:
        return db.query(User).filter(User.username == username).first()

    @staticmethod
    def get_user_by_email(db: Session, email: str) -> User | None:
        return db.query(User).filter(User.email == email).first()

    @staticmethod
    def get_users(
        db: Session, skip: int = 0, limit: int = 100, active_only: bool = True, q: str = None
    ) -> list[User]:
        query = db.query(User)
        if active_only:
            query = query.filter(User.is_active == True)
        if q:
            search_term = f"%{q}%"
            query = query.filter(
                (User.username.ilike(search_term)) | (User.email.ilike(search_term))
            )
        return query.offset(skip).limit(limit).all()

    @staticmethod
    def update_user(db: Session, user_id: int, user_update: UserUpdate) -> User | None:
        try:
            with db.begin_nested():
                db_user = db.query(User).filter(User.id == user_id).first()
                if not db_user:
                    return None
                if user_update.username and user_update.username != db_user.username:
                    if (
                        db.query(User)
                        .filter(User.username == user_update.username)
                        .first()
                    ):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Username already taken",
                        )
                if user_update.email and user_update.email != db_user.email:
                    if db.query(User).filter(User.email == user_update.email).first():
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Email already taken",
                        )
                if user_update.role_id:
                    role = db.query(Role).filter(Role.id == user_update.role_id).first()
                    if not role:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Role not found",
                        )
                update_data = user_update.dict(exclude_unset=True)
                # If password present in update (not in current schema, but future-proof), enforce policy
                new_password = update_data.pop("password", None)
                if new_password:
                    UserService._enforce_password_policy(
                        db, db_user.username, db_user.email, new_password
                    )
                    db_user.hashed_password = get_password_hash(new_password)
                for field, value in update_data.items():
                    setattr(db_user, field, value)
                db.flush()

            db.commit()
            db.refresh(db_user)
            return db_user
        except Exception as e:
            db.rollback()
            raise e

    @staticmethod
    def delete_user(db: Session, user_id: int) -> bool:
        db_user = db.query(User).filter(User.id == user_id).first()
        if not db_user:
            return False
        db_user.is_active = False
        db.commit()
        return True

    @staticmethod
    def authenticate_user(db: Session, username: str, password: str) -> User | None:
        user = (
            db.query(User)
            .filter(and_(User.username == username, User.is_active == True))
            .first()
        )
        if not user:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user

    @staticmethod
    def change_password(
        db: Session, user_id: int, current_password: str, new_password: str
    ) -> bool:
        try:
            with db.begin_nested():
                db_user = db.query(User).filter(User.id == user_id).first()
                if not db_user:
                    return False
                if not verify_password(current_password, db_user.hashed_password):
                    return False
                # Enforce password policy
                UserService._enforce_password_policy(
                    db, db_user.username, db_user.email, new_password
                )
                db_user.hashed_password = get_password_hash(new_password)
                db.flush()

            db.commit()
            return True
        except Exception as e:
            db.rollback()
            raise e

    @staticmethod
    def _enforce_password_policy(
        db: Session, username: str, email: str, password: str
    ) -> None:
        """Validate password against policy; raise HTTPException on failure."""
        policy = db.query(PasswordPolicy).first()
        # Defaults if not set
        if not policy:
            policy = PasswordPolicy()
            db.add(policy)
            db.flush()
            db.refresh(policy)

        pwd = password or ""

        # 1. Minimum Length
        if len(pwd) < (policy.min_length or 0):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Password must be at least {policy.min_length} characters",
            )

        # 2. Common Password Check (Simple Dictionary)
        common_passwords = {
            "password",
            "12345678",
            "123456789",
            "123456",
            "qwerty",
            "admin",
            "welcome",
            "login",
            "changeme",
            "monitor",
            "camera",
            "security",
            "opennvr",
            "nvr",
        }
        if pwd.lower() in common_passwords or pwd.lower() in [
            "12345678",
            "password123",
        ]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password is too common/weak",
            )

        # 3. Username/Email Check
        if policy.disallow_username_email:
            low = pwd.lower()
            if username and username.lower() in low:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Password must not contain the username",
                )
            if email and email.split("@")[0].lower() in low:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Password must not contain the email local part",
                )

        # 4. Passphrase Exception (Long passwords can skip complexity checks)
        # If passphrase is enabled and checks met, we return success immediately
        if policy.passphrase_enabled and len(pwd) >= (
            policy.passphrase_min_length or 0
        ):
            return

        # 5. Complexity (Character Classes)
        classes = 0
        if any(c.islower() for c in pwd):
            classes += 1
        if any(c.isupper() for c in pwd):
            classes += 1
        if any(c.isdigit() for c in pwd):
            classes += 1
        if any(not c.isalnum() for c in pwd):
            classes += 1

        if classes < (policy.min_classes or 1):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Password must include at least {policy.min_classes} of: lowercase, uppercase, number, special",
            )
