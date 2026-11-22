import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, throwError } from 'rxjs';
import {
  CalendarEvent,
  CallHistoryDetail,
  CallHistoryPage,
  LineStatus,
  RealCallTicket,
  StartCallRequest,
  TurnExecutionResponse,
  VoiceTargetInput,
} from '../models/call-agent.models';

@Injectable({
  providedIn: 'root',
})
export class CallAgentApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = 'http://127.0.0.1:8015';

  stepCall(payload: StartCallRequest): Observable<TurnExecutionResponse> {
    const url = `${this.baseUrl.replace(/\/+$/, '')}/api/v1/calls/step`;
    return this.http
      .post<TurnExecutionResponse>(url, payload)
      .pipe(catchError((err: HttpErrorResponse) => this.handleError(err)));
  }

  // ------------------------------------------------------------ llamadas reales
  lineStatus(): Observable<LineStatus> {
    return this.get<LineStatus>('/api/v1/telephony/status');
  }

  startRealCall(target: VoiceTargetInput, modelName: string | null): Observable<RealCallTicket> {
    return this.http
      .post<RealCallTicket>(this.url('/api/v1/telephony/calls'), { target, model_name: modelName })
      .pipe(catchError((err: HttpErrorResponse) => this.handleError(err)));
  }

  realCall(ticketId: string): Observable<RealCallTicket> {
    return this.get<RealCallTicket>(`/api/v1/telephony/tickets/${encodeURIComponent(ticketId)}`);
  }

  /** Historial permanente: las más nuevas primero; q busca en clínica, resultado y frases. */
  callHistory(limit = 15, offset = 0, q = ''): Observable<CallHistoryPage> {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset), q });
    return this.get<CallHistoryPage>(`/api/v1/telephony/calls?${query.toString()}`);
  }

  deleteCall(voiceId: string): Observable<{ ok: boolean }> {
    return this.http
      .delete<{ ok: boolean }>(this.url(`/api/v1/telephony/calls/${encodeURIComponent(voiceId)}`))
      .pipe(catchError((err: HttpErrorResponse) => this.handleError(err)));
  }

  deleteAllCalls(): Observable<{ ok: boolean; borradas: number }> {
    return this.http
      .delete<{ ok: boolean; borradas: number }>(this.url('/api/v1/telephony/calls?confirm=true'))
      .pipe(catchError((err: HttpErrorResponse) => this.handleError(err)));
  }

  callDetail(voiceId: string): Observable<CallHistoryDetail> {
    return this.get<CallHistoryDetail>(`/api/v1/telephony/calls/${encodeURIComponent(voiceId)}`);
  }

  calendarEvents(): Observable<CalendarEvent[]> {
    return this.get<CalendarEvent[]>('/api/v1/calendar/events');
  }

  private url(path: string): string {
    return `${this.baseUrl.replace(/\/+$/, '')}${path}`;
  }

  private get<T>(path: string): Observable<T> {
    return this.http
      .get<T>(this.url(path))
      .pipe(catchError((err: HttpErrorResponse) => this.handleError(err)));
  }

  private handleError(err: HttpErrorResponse): Observable<never> {
    const serverDetail =
      typeof err.error?.detail === 'string'
        ? err.error.detail
        : `Error HTTP ${err.status || 0} al contactar el backend en ${this.baseUrl}.`;
    return throwError(() => new Error(serverDetail));
  }
}