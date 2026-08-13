import logging
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import select, update, and_
from database.models import DriverPreference, PendingOffer
from database.connection import get_session

logger = logging.getLogger(__name__)


class DriverPreferenceRepo:

    def get(self, driver_id: int) -> Optional[DriverPreference]:
        with get_session() as session:
            return session.get(DriverPreference, driver_id)

    def upsert(self, driver_id: int, chat_id: str, **kwargs) -> DriverPreference:
        with get_session() as session:
            pref = session.get(DriverPreference, driver_id)
            if not pref:
                pref = DriverPreference(driver_id=driver_id, chat_id=chat_id)
                session.add(pref)
            for k, v in kwargs.items():
                setattr(pref, k, v)
            pref.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(pref)
            return pref

    def is_expired(self, pref: DriverPreference) -> bool:
        if not pref.preference_expires:
            return False
        return datetime.now(timezone.utc) > pref.preference_expires

    def is_available(self, driver_id: int) -> bool:
        pref = self.get(driver_id)
        if not pref:
            return True
        if self.is_expired(pref):
            return True
        return pref.available

    def get_effective(self, driver_id: int) -> Optional[DriverPreference]:
        """Returns pref only if not expired, else None."""
        pref = self.get(driver_id)
        if not pref:
            return None
        if self.is_expired(pref):
            return None
        return pref


class PendingOfferRepo:

    def create(
        self,
        driver_id: int,
        load_id: str,
        load_snapshot: dict,
        expires_at: datetime,
        telegram_message_id: Optional[int] = None,
    ) -> PendingOffer:
        with get_session() as session:
            offer = PendingOffer(
                driver_id=driver_id,
                load_id=load_id,
                load_snapshot=load_snapshot,
                expires_at=expires_at,
                telegram_message_id=telegram_message_id,
            )
            session.add(offer)
            session.commit()
            session.refresh(offer)
            return offer

    def get_by_message_id(self, message_id: int) -> Optional[PendingOffer]:
        """The offer a driver is replying to, whether or not it has expired."""
        with get_session() as session:
            stmt = select(PendingOffer).where(
                PendingOffer.telegram_message_id == message_id
            )
            return session.execute(stmt).scalars().first()

    def get_active(self, driver_id: int) -> Optional[PendingOffer]:
        """Get active (non-expired, sent) offer for a driver."""
        with get_session() as session:
            stmt = select(PendingOffer).where(
                and_(
                    PendingOffer.driver_id == driver_id,
                    PendingOffer.status == 'sent',
                    PendingOffer.expires_at > datetime.now(timezone.utc),
                )
            ).order_by(PendingOffer.created_at.desc())
            return session.execute(stmt).scalars().first()

    def update_status(self, offer_id, status: str):
        with get_session() as session:
            session.execute(
                update(PendingOffer)
                .where(PendingOffer.id == offer_id)
                .values(status=status, responded_at=datetime.now(timezone.utc))
            )
            session.commit()

    def expire_old(self):
        with get_session() as session:
            session.execute(
                update(PendingOffer)
                .where(
                    and_(
                        PendingOffer.status == 'sent',
                        PendingOffer.expires_at <= datetime.now(timezone.utc),
                    )
                )
                .values(status='expired')
            )
            session.commit()
            logger.debug("Expired old pending offers")
            