from datetime import datetime, timezone
from uuid import uuid4

from .config import effective_dry_run, settings
from .schemas import RtpMediaState, SipRtpSessionStatus, SipSignalState, VoiceTargetInput


class SipRtpBridge:
    """Desacopla la máquina de estados SIP/RTP del modelo de lenguaje."""

    def __init__(self) -> None:
        self._sessions: dict[str, SipRtpSessionStatus] = {}

    def open_session(self, target: VoiceTargetInput) -> SipRtpSessionStatus:
        session_id = f"sip_{uuid4().hex[:12]}"
        simulated = effective_dry_run(target.dry_run)

        sip_state = (
            SipSignalState.ESTABLISHED_200_OK
            if simulated
            else SipSignalState.INVITING
        )
        rtp_state = (
            RtpMediaState.STREAMING_PCMU
            if simulated
            else RtpMediaState.NEGOTIATING_SDP
        )

        status = SipRtpSessionStatus(
            session_id=session_id,
            sip_state=sip_state,
            rtp_state=rtp_state,
            codec=settings.rtp_codec,
            transport=settings.sip_transport,
            rtp_packets_sent=12 if simulated else 0,
            rtp_packets_received=8 if simulated else 0,
            last_sip_method="INVITE/200_OK/ACK" if simulated else "INVITE",
            updated_at=datetime.now(timezone.utc),
        )
        self._sessions[session_id] = status
        return status

    def record_turn_packets(self, session_id: str, outgoing_chunks: int = 10, incoming_chunks: int = 10) -> SipRtpSessionStatus:
        current = self._sessions[session_id]
        updated = current.model_copy(
            update={
                "rtp_packets_sent": current.rtp_packets_sent + max(0, outgoing_chunks),
                "rtp_packets_received": current.rtp_packets_received + max(0, incoming_chunks),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._sessions[session_id] = updated
        return updated

    def terminate_session(self, session_id: str) -> SipRtpSessionStatus:
        current = self._sessions[session_id]
        updated = current.model_copy(
            update={
                "sip_state": SipSignalState.TERMINATED,
                "rtp_state": RtpMediaState.CLOSED,
                "last_sip_method": "BYE",
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._sessions[session_id] = updated
        return updated

    def get_session(self, session_id: str) -> SipRtpSessionStatus | None:
        return self._sessions.get(session_id)