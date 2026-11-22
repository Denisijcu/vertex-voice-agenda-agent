import { DatePipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  computed,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { take } from 'rxjs';
import {
  CalendarEvent,
  LineStatus,
  RealCallTicket,
  VoiceTargetInput,
} from '../../core/models/call-agent.models';
import { CallAgentApiService } from '../../core/services/call-agent-api.service';

/**
 * Llamada de voz real: la API le pide a Asterisk que marque al celular (Linphone) y la pasarela hace la
 * conversación con la ficha de este formulario. Aquí solo se muestra: estado de la línea, la llamada en vivo,
 * el historial y la agenda. Todo por sondeo (polling) y solo en el navegador (la app tiene SSR).
 */
@Component({
  selector: 'app-real-call-panel',
  standalone: true,
  imports: [DatePipe, MatButtonModule, MatCardModule, MatIconModule, MatProgressBarModule],
  templateUrl: './real-call-panel.component.html',
  styleUrl: './real-call-panel.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RealCallPanelComponent {
  private readonly api = inject(CallAgentApiService);

  /** Ficha de la llamada: sale del formulario de la consola. */
  readonly target = input.required<VoiceTargetInput>();
  /** Vacío = el modelo por defecto de la API. */
  readonly modelName = input<string>('');

  readonly line = signal<LineStatus | null>(null);
  readonly lineError = signal<string | null>(null);
  readonly ticket = signal<RealCallTicket | null>(null);
  readonly starting = signal(false);
  readonly callError = signal<string | null>(null);
  /** La API se reinició durante la llamada: se espera al final, cuando la pasarela manda la transcripción entera. */
  readonly lost = signal(false);
  private lostSince = 0;
  readonly agenda = signal<CalendarEvent[]>([]);

  readonly active = computed(() => {
    const estado = this.ticket()?.estado;
    return estado === 'llamando' || estado === 'en_curso';
  });
  readonly canCall = computed(
    () =>
      !!this.line()?.ready &&
      !this.active() &&
      !this.starting() &&
      this.target().contact_name.trim().length > 0
  );
  readonly events = computed(() => this.ticket()?.call?.events ?? []);
  readonly upcoming = computed(() => {
    const ahora = Date.now() - 60 * 60 * 1000;
    return this.agenda()
      .filter((e) => new Date(e.end_iso).getTime() >= ahora)
      .sort((a, b) => a.start_iso.localeCompare(b.start_iso))
      .slice(0, 8);
  });

  private readonly transcriptBox = viewChild<ElementRef<HTMLElement>>('transcriptBox');

  private timers: ReturnType<typeof setInterval>[] = [];
  private busy = { line: false, ticket: false, lists: false };

  constructor() {
    const destroyRef = inject(DestroyRef);
    afterNextRender(() => {
      this.refreshLine();
      this.refreshLists();
      this.timers.push(setInterval(() => this.refreshLine(), 3000));
      this.timers.push(setInterval(() => this.refreshTicket(), 1000));
      this.timers.push(setInterval(() => this.refreshLists(), 6000));
    });
    destroyRef.onDestroy(() => this.timers.forEach((t) => clearInterval(t)));
  }

  callNow(): void {
    if (!this.canCall()) {
      return;
    }
    this.starting.set(true);
    this.callError.set(null);
    const target = { ...this.target(), dry_run: this.target().dry_run };
    this.api
      .startRealCall(target, this.modelName().trim() || null)
      .pipe(take(1))
      .subscribe({
        next: (t) => {
          this.lost.set(false);
          this.ticket.set({ ticket_id: t.ticket_id, estado: 'llamando', call: null });
          this.starting.set(false);
        },
        error: (err: unknown) => {
          this.callError.set(err instanceof Error ? err.message : 'No se pudo lanzar la llamada.');
          this.starting.set(false);
          this.refreshLine();
        },
      });
  }

  closeCall(): void {
    this.lost.set(false);
    this.ticket.set(null);
    this.callError.set(null);
  }

  phoneLabel(phone: string | null): string {
    if (phone === 'Avail') return 'Teléfono de la clínica listo';
    if (phone === null) return 'Teléfono de la clínica sin registrar';
    if (phone === 'Unavail') return 'Teléfono de la clínica dormido';
    return `Teléfono de la clínica: ${phone}`;
  }

  stateLabel(estado: RealCallTicket['estado']): string {
    return {
      llamando: 'Sonando en la clínica…',
      en_curso: 'En curso',
      terminada: 'Terminada',
      sin_contestar: 'La clínica no contestó',
    }[estado];
  }

  speakerLabel(quien: string, clinica?: string | null): string {
    if (quien === 'agente') return 'Asistente';
    if (quien === 'contraparte') return clinica || 'Clínica';
    return 'Sistema';
  }

  /** Baja la transcripción a la última frase (si el usuario no se ha ido muy arriba a leer). */
  private scrollToEnd(): void {
    setTimeout(() => {
      const el = this.transcriptBox()?.nativeElement;
      if (!el) return;
      const cerca = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
      if (cerca || this.events().length <= 3) el.scrollTop = el.scrollHeight;
    });
  }

  private refreshLine(): void {
    if (this.busy.line) return;
    this.busy.line = true;
    this.api
      .lineStatus()
      .pipe(take(1))
      .subscribe({
        next: (s) => {
          this.line.set(s);
          this.lineError.set(null);
          this.busy.line = false;
        },
        error: (err: unknown) => {
          this.line.set(null);
          this.lineError.set(err instanceof Error ? err.message : 'La API no responde.');
          this.busy.line = false;
        },
      });
  }

  private refreshTicket(): void {
    const actual = this.ticket();
    if (!actual || !this.active() || this.busy.ticket) return;
    this.busy.ticket = true;
    this.api
      .realCall(actual.ticket_id)
      .pipe(take(1))
      .subscribe({
        next: (t) => {
          const antes = this.ticket()?.estado;
          const frasesAntes = this.events().length;
          this.ticket.set(t);
          if ((t.call?.events?.length ?? 0) > frasesAntes) this.scrollToEnd();
          this.lost.set(false);
          this.busy.ticket = false;
          if (antes !== t.estado && (t.estado === 'terminada' || t.estado === 'sin_contestar')) {
            this.refreshLists();
          }
        },
        error: (err: unknown) => {
          this.busy.ticket = false;
          const msg = err instanceof Error ? err.message : '';
          if (!msg.includes('no existe')) return;
          // 404: la API se reinició y perdió la llamada en memoria. Al colgar, la pasarela la reconstruye.
          if (!this.lost()) {
            this.lost.set(true);
            this.lostSince = Date.now();
          } else if (Date.now() - this.lostSince > 15 * 60 * 1000) {
            const actual = this.ticket();
            if (actual) this.ticket.set({ ...actual, estado: 'terminada' });
          }
        },
      });
  }

  private refreshLists(): void {
    if (this.busy.lists) return;
    this.busy.lists = true;
    const listo = () => {
      this.busy.lists = false;
    };
    this.api
      .calendarEvents()
      .pipe(take(1))
      .subscribe({ next: (a) => (this.agenda.set(a), listo()), error: listo });
  }
}
