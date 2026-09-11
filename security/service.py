from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Optional

from config import Config
from database import Database, SecurityUserStateRecord


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RateDecision:
    allowed: bool
    reason: str = ""
    retry_after: float = 0.0
    auto_banned: bool = False
    ban_until: Optional[datetime] = None


@dataclass(slots=True)
class AccessDecision:
    allowed: bool
    reason: str = ""
    ban_until: Optional[datetime] = None
    permanent: bool = False


@dataclass(slots=True)
class SecurityStatus:
    access_mode: str
    env_allowlist_count: int
    db_allowlist_count: int
    effective_allowlist_count: int
    active_bans: int
    permanent_bans: int
    tracked_security_users: int


@dataclass(slots=True)
class _UserState:
    user_id: int
    strikes: int = 0
    last_strike_at: Optional[datetime] = None
    auto_ban_level: int = 0
    ban_until: Optional[datetime] = None
    ban_reason: Optional[str] = None
    permanent_ban: bool = False

    @classmethod
    def from_record(cls, row: SecurityUserStateRecord) -> "_UserState":
        return cls(
            user_id=row.user_id,
            strikes=row.strikes,
            last_strike_at=_parse_iso(row.last_strike_at),
            auto_ban_level=row.auto_ban_level,
            ban_until=_parse_iso(row.ban_until),
            ban_reason=row.ban_reason,
            permanent_ban=row.permanent_ban,
        )


@dataclass(slots=True)
class _WindowCounter:
    events: Deque[tuple[float, int]] = field(default_factory=deque)
    total: int = 0
    last_seen: float = 0.0

    def prune(self, now: float, window: float) -> None:
        cutoff = now - window
        while self.events and self.events[0][0] <= cutoff:
            _, units = self.events.popleft()
            self.total -= units
        if self.total < 0:
            self.total = 0
        self.last_seen = now

    def retry_after(self, now: float, window: float) -> float:
        if not self.events:
            return 0.0
        return max(0.0, self.events[0][0] + window - now)

    def consume(self, now: float, units: int) -> None:
        self.events.append((now, units))
        self.total += units
        self.last_seen = now


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


class SecurityService:
    """Anti-abuse, ban/allowlist та persistent access mode.

    Rate buckets зберігаються у пам'яті й очищаються TTL-логікою. Бан, strikes,
    whitelist та access mode зберігаються у SQLite й переживають restart.
    """

    ACCESS_MODES = {"public", "whitelist", "private"}

    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database
        self._rate_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._states: dict[int, _UserState] = {}
        self._db_allowlist: set[int] = set()
        self._access_mode = config.SECURITY_DEFAULT_ACCESS_MODE
        self._user_buckets: dict[tuple[str, int], _WindowCounter] = {}
        self._global_buckets: dict[str, _WindowCounter] = {}
        self._notice_times: dict[tuple[int, str], float] = {}
        self._last_strike_monotonic: dict[int, float] = {}
        self._checks_since_cleanup = 0

    async def initialize(self) -> None:
        async with self._state_lock:
            rows = await self.database.load_security_user_states()
            self._states = {row.user_id: _UserState.from_record(row) for row in rows}
            self._db_allowlist = await self.database.load_security_allowlist()
            stored_mode = await self.database.get_security_setting("access_mode")
            if stored_mode in self.ACCESS_MODES:
                self._access_mode = stored_mode
            else:
                self._access_mode = self.config.SECURITY_DEFAULT_ACCESS_MODE
                await self.database.set_security_setting("access_mode", self._access_mode)

        logger.info(
            "Security v3 ініціалізовано: mode=%s allowlist=%s security_states=%s",
            self._access_mode,
            len(self.effective_allowlist),
            len(self._states),
        )

    @property
    def access_mode(self) -> str:
        return self._access_mode

    @property
    def effective_allowlist(self) -> set[int]:
        return set(self.config.SECURITY_WHITELIST_IDS) | set(self._db_allowlist)

    def is_admin(self, user_id: int) -> bool:
        return self.config.ADMIN_ID > 0 and user_id == self.config.ADMIN_ID

    async def check_access(self, user_id: int) -> AccessDecision:
        if self.is_admin(user_id):
            return AccessDecision(True)

        now = datetime.now(timezone.utc)
        async with self._state_lock:
            state = self._states.get(user_id)
            if state is not None:
                if state.permanent_ban:
                    return AccessDecision(
                        False,
                        state.ban_reason or "Доступ заблоковано адміністратором.",
                        permanent=True,
                    )
                if state.ban_until is not None:
                    if state.ban_until > now:
                        return AccessDecision(
                            False,
                            state.ban_reason or "Доступ тимчасово заблоковано.",
                            ban_until=state.ban_until,
                        )
                    # Тимчасовий ban закінчився. Рівень auto-ban лишаємо,
                    # але активний ban очищаємо один раз у БД.
                    state.ban_until = None
                    state.ban_reason = None
                    state.permanent_ban = False
                    await self._persist_state(state)

            if self._access_mode == "private":
                return AccessDecision(False, "Бот тимчасово працює у приватному режимі.")
            if self._access_mode == "whitelist" and user_id not in self.effective_allowlist:
                return AccessDecision(False, "Доступ до бота дозволено лише whitelist-користувачам.")

        return AccessDecision(True)

    async def check_update(self, user_id: int, *, is_callback: bool) -> RateDecision:
        if self.is_admin(user_id):
            return RateDecision(True)

        now = time.monotonic()
        user_specs = [
            (
                "general",
                self.config.SECURITY_GENERAL_USER_LIMIT,
                self.config.SECURITY_GENERAL_WINDOW_SECONDS,
            ),
            (
                "burst",
                self.config.SECURITY_BURST_USER_LIMIT,
                self.config.SECURITY_BURST_WINDOW_SECONDS,
            ),
        ]
        if is_callback:
            user_specs.append(
                (
                    "callback",
                    self.config.SECURITY_CALLBACK_USER_LIMIT,
                    self.config.SECURITY_CALLBACK_WINDOW_SECONDS,
                )
            )

        async with self._rate_lock:
            denied_name = ""
            retry_after = 0.0
            for name, limit, window in user_specs:
                bucket = self._user_buckets.setdefault((name, user_id), _WindowCounter())
                bucket.prune(now, float(window))
                if bucket.total + 1 > limit:
                    denied_name = name
                    retry_after = max(retry_after, bucket.retry_after(now, float(window)))
                    break

            if not denied_name:
                global_bucket = self._global_buckets.setdefault("updates", _WindowCounter())
                global_bucket.prune(now, float(self.config.SECURITY_GLOBAL_UPDATE_WINDOW_SECONDS))
                if global_bucket.total + 1 > self.config.SECURITY_GLOBAL_UPDATE_LIMIT:
                    return RateDecision(
                        False,
                        reason="global_updates",
                        retry_after=global_bucket.retry_after(
                            now, float(self.config.SECURITY_GLOBAL_UPDATE_WINDOW_SECONDS)
                        ),
                    )

                for name, _, _ in user_specs:
                    self._user_buckets[(name, user_id)].consume(now, 1)
                global_bucket.consume(now, 1)
                self._maybe_cleanup_rate_state(now)
                return RateDecision(True)

            self._maybe_cleanup_rate_state(now)

        return await self._user_rate_violation(
            user_id=user_id,
            reason=f"update:{denied_name}",
            retry_after=retry_after,
        )

    async def check_action(
        self,
        user_id: int,
        action: str,
        *,
        units: int = 1,
    ) -> RateDecision:
        if self.is_admin(user_id):
            return RateDecision(True)
        units = max(1, int(units))

        if action == "search":
            user_limit = self.config.SECURITY_SEARCH_USER_LIMIT
            user_window = self.config.SECURITY_SEARCH_WINDOW_SECONDS
            global_limit = self.config.SECURITY_SEARCH_GLOBAL_LIMIT
            global_window = self.config.SECURITY_SEARCH_GLOBAL_WINDOW_SECONDS
        elif action == "playlist":
            user_limit = self.config.SECURITY_PLAYLIST_USER_LIMIT
            user_window = self.config.SECURITY_PLAYLIST_WINDOW_SECONDS
            global_limit = self.config.SECURITY_PLAYLIST_GLOBAL_LIMIT
            global_window = self.config.SECURITY_PLAYLIST_GLOBAL_WINDOW_SECONDS
        elif action == "download":
            user_limit = self.config.SECURITY_DOWNLOAD_USER_LIMIT
            user_window = self.config.SECURITY_DOWNLOAD_WINDOW_SECONDS
            global_limit = self.config.SECURITY_DOWNLOAD_GLOBAL_LIMIT
            global_window = self.config.SECURITY_DOWNLOAD_GLOBAL_WINDOW_SECONDS
        else:
            raise ValueError(f"Невідомий security action: {action}")

        now = time.monotonic()
        async with self._rate_lock:
            user_bucket = self._user_buckets.setdefault((action, user_id), _WindowCounter())
            user_bucket.prune(now, float(user_window))
            if user_bucket.total + units > user_limit:
                retry_after = user_bucket.retry_after(now, float(user_window))
                self._maybe_cleanup_rate_state(now)
            else:
                global_bucket = self._global_buckets.setdefault(action, _WindowCounter())
                global_bucket.prune(now, float(global_window))
                if global_bucket.total + units > global_limit:
                    retry_after = global_bucket.retry_after(now, float(global_window))
                    self._maybe_cleanup_rate_state(now)
                    return RateDecision(
                        False,
                        reason=f"global_{action}",
                        retry_after=retry_after,
                    )

                user_bucket.consume(now, units)
                global_bucket.consume(now, units)
                self._maybe_cleanup_rate_state(now)
                return RateDecision(True)

        return await self._user_rate_violation(
            user_id=user_id,
            reason=action,
            retry_after=retry_after,
        )

    async def _user_rate_violation(
        self,
        *,
        user_id: int,
        reason: str,
        retry_after: float,
    ) -> RateDecision:
        auto_banned = False
        ban_until: Optional[datetime] = None
        now_mono = time.monotonic()

        async with self._state_lock:
            previous = self._last_strike_monotonic.get(user_id, 0.0)
            if now_mono - previous >= self.config.SECURITY_STRIKE_COOLDOWN_SECONDS:
                self._last_strike_monotonic[user_id] = now_mono
                now = datetime.now(timezone.utc)
                state = self._states.setdefault(user_id, _UserState(user_id=user_id))

                if (
                    state.last_strike_at is None
                    or (now - state.last_strike_at).total_seconds()
                    > self.config.SECURITY_STRIKE_DECAY_SECONDS
                ):
                    state.strikes = 0

                state.strikes += 1
                state.last_strike_at = now

                if state.strikes >= self.config.SECURITY_STRIKES_BEFORE_BAN:
                    durations = self.config.SECURITY_AUTOBAN_MINUTES
                    index = min(state.auto_ban_level, len(durations) - 1)
                    minutes = durations[index]
                    state.auto_ban_level = min(state.auto_ban_level + 1, len(durations) - 1)
                    state.strikes = 0
                    state.permanent_ban = False
                    state.ban_until = now + timedelta(minutes=minutes)
                    state.ban_reason = f"Автоматичний ban за flood/spam ({reason})"
                    auto_banned = True
                    ban_until = state.ban_until
                    logger.warning(
                        "Security auto-ban user_id=%s minutes=%s reason=%s level=%s",
                        user_id,
                        minutes,
                        reason,
                        state.auto_ban_level,
                    )
                else:
                    logger.warning(
                        "Security strike user_id=%s strikes=%s/%s reason=%s",
                        user_id,
                        state.strikes,
                        self.config.SECURITY_STRIKES_BEFORE_BAN,
                        reason,
                    )

                await self._persist_state(state)

        return RateDecision(
            False,
            reason=reason,
            retry_after=max(0.0, retry_after),
            auto_banned=auto_banned,
            ban_until=ban_until,
        )

    async def ban_user(self, user_id: int, minutes: int, reason: str) -> _UserState:
        if user_id <= 0:
            raise ValueError("USER_ID має бути додатним")
        if self.is_admin(user_id):
            raise ValueError("ADMIN_ID не можна заблокувати")
        minutes = int(minutes)
        if minutes < 0:
            raise ValueError("MINUTES не може бути від'ємним")
        reason = " ".join((reason or "Заблоковано адміністратором").split())[:300]

        async with self._state_lock:
            state = self._states.setdefault(user_id, _UserState(user_id=user_id))
            state.strikes = 0
            state.last_strike_at = None
            state.permanent_ban = minutes == 0
            state.ban_until = None if minutes == 0 else datetime.now(timezone.utc) + timedelta(minutes=minutes)
            state.ban_reason = reason
            await self._persist_state(state)
            return state

    async def unban_user(self, user_id: int) -> None:
        if user_id <= 0:
            raise ValueError("USER_ID має бути додатним")
        async with self._state_lock:
            state = self._states.setdefault(user_id, _UserState(user_id=user_id))
            state.strikes = 0
            state.last_strike_at = None
            state.auto_ban_level = 0
            state.ban_until = None
            state.ban_reason = None
            state.permanent_ban = False
            await self._persist_state(state)

    async def set_allowlisted(self, user_id: int, allowed: bool, *, added_by: int) -> None:
        if user_id <= 0:
            raise ValueError("USER_ID має бути додатним")
        if self.is_admin(user_id):
            raise ValueError("ADMIN_ID завжди має доступ і не керується whitelist")
        async with self._state_lock:
            await self.database.set_security_allowlist(
                user_id=user_id,
                allowed=allowed,
                added_by=added_by,
            )
            if allowed:
                self._db_allowlist.add(user_id)
            else:
                self._db_allowlist.discard(user_id)

    async def set_access_mode(self, mode: str) -> None:
        normalized = mode.strip().lower()
        if normalized not in self.ACCESS_MODES:
            raise ValueError("Режим має бути public, whitelist або private")
        async with self._state_lock:
            await self.database.set_security_setting("access_mode", normalized)
            self._access_mode = normalized
        logger.warning("Security access mode змінено на %s", normalized)

    async def status(self) -> SecurityStatus:
        now = datetime.now(timezone.utc)
        async with self._state_lock:
            active = 0
            permanent = 0
            for state in self._states.values():
                if state.permanent_ban:
                    active += 1
                    permanent += 1
                elif state.ban_until is not None and state.ban_until > now:
                    active += 1
            return SecurityStatus(
                access_mode=self._access_mode,
                env_allowlist_count=len(self.config.SECURITY_WHITELIST_IDS),
                db_allowlist_count=len(self._db_allowlist),
                effective_allowlist_count=len(self.effective_allowlist),
                active_bans=active,
                permanent_bans=permanent,
                tracked_security_users=len(self._states),
            )

    async def should_notify(self, user_id: int, category: str, *, cooldown: Optional[int] = None) -> bool:
        if self.is_admin(user_id):
            return True
        now = time.monotonic()
        seconds = (
            self.config.SECURITY_BAN_NOTICE_COOLDOWN_SECONDS
            if cooldown is None
            else max(1, int(cooldown))
        )
        key = (user_id, category)
        async with self._rate_lock:
            previous = self._notice_times.get(key, 0.0)
            if now - previous < seconds:
                return False
            self._notice_times[key] = now
            if len(self._notice_times) > 10_000:
                cutoff = now - max(300, seconds * 10)
                self._notice_times = {
                    item_key: ts for item_key, ts in self._notice_times.items() if ts >= cutoff
                }
            return True

    def format_access_denial(self, decision: AccessDecision) -> str:
        if decision.permanent:
            return f"⛔ Доступ заблоковано. {decision.reason}"[:400]
        if decision.ban_until is not None:
            local = decision.ban_until.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            return f"⛔ Тимчасове блокування до {local}. {decision.reason}"[:400]
        return f"⛔ {decision.reason}"[:400]

    def format_rate_denial(self, decision: RateDecision) -> str:
        if decision.auto_banned and decision.ban_until is not None:
            local = decision.ban_until.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            return f"⛔ Flood/spam захист: тимчасове блокування до {local}."
        if decision.reason.startswith("global_"):
            return "⚠️ Бот зараз отримує забагато запитів. Спробуйте трохи пізніше."
        wait = max(1, int(decision.retry_after + 0.99)) if decision.retry_after > 0 else 1
        return f"⏳ Забагато запитів. Спробуйте приблизно через {wait} сек."

    async def _persist_state(self, state: _UserState) -> None:
        await self.database.save_security_user_state(
            user_id=state.user_id,
            strikes=state.strikes,
            last_strike_at=_iso(state.last_strike_at),
            auto_ban_level=state.auto_ban_level,
            ban_until=_iso(state.ban_until),
            ban_reason=state.ban_reason,
            permanent_ban=state.permanent_ban,
        )

    def _maybe_cleanup_rate_state(self, now: float) -> None:
        self._checks_since_cleanup += 1
        if self._checks_since_cleanup < 500:
            return
        self._checks_since_cleanup = 0
        longest = float(
            max(
                self.config.SECURITY_GENERAL_WINDOW_SECONDS,
                self.config.SECURITY_BURST_WINDOW_SECONDS,
                self.config.SECURITY_CALLBACK_WINDOW_SECONDS,
                self.config.SECURITY_SEARCH_WINDOW_SECONDS,
                self.config.SECURITY_PLAYLIST_WINDOW_SECONDS,
                self.config.SECURITY_DOWNLOAD_WINDOW_SECONDS,
                self.config.SECURITY_GLOBAL_UPDATE_WINDOW_SECONDS,
                self.config.SECURITY_SEARCH_GLOBAL_WINDOW_SECONDS,
                self.config.SECURITY_PLAYLIST_GLOBAL_WINDOW_SECONDS,
                self.config.SECURITY_DOWNLOAD_GLOBAL_WINDOW_SECONDS,
            )
        )
        cutoff = now - max(600.0, longest * 2.0)
        self._user_buckets = {
            key: bucket for key, bucket in self._user_buckets.items() if bucket.last_seen >= cutoff
        }
        self._global_buckets = {
            key: bucket for key, bucket in self._global_buckets.items() if bucket.last_seen >= cutoff
        }
        self._last_strike_monotonic = {
            uid: ts for uid, ts in self._last_strike_monotonic.items() if ts >= cutoff
        }
