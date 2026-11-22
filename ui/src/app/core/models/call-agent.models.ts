export type CallPurpose =
  | 'dentist_appointment'
  | 'employer_outreach'
  | 'client_marketing'
  | 'follow_up';

export type CallOutcome =
  | 'pending'
  | 'appointment_booked'
  | 'interview_or_role_confirmed'
  | 'voicemail_left'
  | 'callback_requested'
  | 'no_answer'
  | 'rejected_or_unavailable';

export interface VoiceTargetInput {
  contact_name: string;
  phone_or_sip_uri: string;
  purpose: CallPurpose;
  context_notes: string;
  preferred_window_start?: string | null;
  preferred_window_end?: string | null;
  dry_run: boolean;
}

export interface StartCallRequest {
  call_id?: string | null;
  /** Vacío = LLM_DEFAULT_MODEL del backend. */
  model_name?: string | null;
  target: VoiceTargetInput;
  /** Obligatoria en los turnos de seguimiento. */
  caller_utterance?: string | null;
  temperature?: number;
  max_tokens?: number;
}

export interface SipRtpSessionStatus {
  session_id: string;
  sip_state: string;
  rtp_state: string;
  codec: string;
  transport: string;
  rtp_packets_sent: number;
  rtp_packets_received: number;
  last_sip_method: string | null;
}

export interface AgendaToolCallItem {
  tool: 'check_calendar' | 'book_appointment' | 'log_call_outcome' | 'hangup_call';
  arguments: Record<string, unknown>;
}

export interface TurnExecutionResponse {
  call_id: string;
  session: SipRtpSessionStatus;
  assistant_reply: string;
  tool_calls: AgendaToolCallItem[];
  dry_run: boolean;
  outcome: CallOutcome;
  terminated: boolean;
  warnings: string[];
}

export interface TranscriptLine {
  speaker: 'agente' | 'contraparte';
  text: string;
  tools: string[];
}

// ---------------------------------------------------------------- llamadas de voz reales (telephony/)
export interface LineStatus {
  asterisk: boolean;
  /** Avail = celular listo; Unavail = dormido; null = no registrado. */
  phone: string | null;
  gateway: boolean;
  sip_user: string;
  ready: boolean;
  detail: string;
}

export interface VoiceEvent {
  t: string;
  quien: 'agente' | 'contraparte' | 'sistema';
  texto: string;
  tools: string[];
  warnings: string[];
}

export interface VoiceCall {
  voice_id: string;
  ticket_id: string | null;
  started: string;
  status: 'en_curso' | 'terminada';
  result: string | null;
  contact_name: string | null;
  purpose: string | null;
  events?: VoiceEvent[];
  turnos?: number;
}

export interface RealCallTicket {
  ticket_id: string;
  /** llamando → en_curso → terminada; sin_contestar si el celular no contestó a tiempo. */
  estado: 'llamando' | 'en_curso' | 'terminada' | 'sin_contestar';
  call: VoiceCall | null;
}

export interface CalendarEvent {
  event_id: string;
  title: string;
  start_iso: string;
  end_iso: string;
  attendee_or_location: string;
  purpose: string;
  dry_run: boolean;
}

/** Historial permanente de llamadas reales (SQLite en la API). */
export interface CallHistoryItem {
  voice_id: string;
  ticket_id: string | null;
  started: string;
  ended: string | null;
  status: 'en_curso' | 'terminada';
  result: string | null;
  contact_name: string | null;
  purpose: string | null;
  /** ui = lanzada desde la UI; pasarela = marcando 100 o llamar.ps1; importada = de telephony/logs. */
  origen: 'ui' | 'pasarela' | 'importada';
  turnos: number;
}

export interface CallHistoryPage {
  total: number;
  items: CallHistoryItem[];
}

export interface CallHistoryDetail extends CallHistoryItem {
  events: VoiceEvent[];
}
