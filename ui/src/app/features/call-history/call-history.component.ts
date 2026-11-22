import { DatePipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  afterNextRender,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { take } from 'rxjs';
import { CallHistoryDetail, CallHistoryItem } from '../../core/models/call-agent.models';
import { CallAgentApiService } from '../../core/services/call-agent-api.service';

const PAGE = 15;

/**
 * Historial permanente de las llamadas reales: lista con búsqueda, transcripción completa al abrir una llamada
 * y descarga en .txt. Los datos viven en la API (SQLite), así que sobreviven a reinicios.
 */
@Component({
  selector: 'app-call-history',
  standalone: true,
  imports: [
    DatePipe,
    FormsModule,
    MatButtonModule,
    MatCardModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatProgressBarModule,
  ],
  templateUrl: './call-history.component.html',
  styleUrl: './call-history.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CallHistoryComponent {
  private readonly api = inject(CallAgentApiService);

  readonly items = signal<CallHistoryItem[]>([]);
  readonly total = signal(0);
  readonly query = signal('');
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly openId = signal<string | null>(null);
  readonly detail = signal<CallHistoryDetail | null>(null);
  readonly detailLoading = signal(false);
  /** Borrado en dos clics: el primero arma la confirmación (id de la llamada o 'todo'), el segundo borra. */
  readonly confirming = signal<string | null>(null);
  private confirmTimer: ReturnType<typeof setTimeout> | null = null;

  private searchTimer: ReturnType<typeof setTimeout> | null = null;
  /** Cada carga lleva un número: si llega una respuesta vieja (p. ej. un refresco de antes de borrar), se ignora. */
  private seq = 0;
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    const destroyRef = inject(DestroyRef);
    afterNextRender(() => {
      this.load(true);
      // Las llamadas nuevas aparecen solas (solo la primera página y sin búsqueda, para no mover lo que lees).
      this.refreshTimer = setInterval(() => {
        if (!this.query().trim() && this.items().length <= PAGE) this.load(true, true);
        const abierta = this.detail();
        if (abierta?.status === 'en_curso') this.open(abierta.voice_id, true);
      }, 8000);
    });
    destroyRef.onDestroy(() => {
      if (this.refreshTimer) clearInterval(this.refreshTimer);
      if (this.searchTimer) clearTimeout(this.searchTimer);
      if (this.confirmTimer) clearTimeout(this.confirmTimer);
    });
  }

  onSearch(value: string): void {
    this.query.set(value);
    if (this.searchTimer) clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => this.load(true), 350);
  }

  loadMore(): void {
    this.load(false);
  }

  toggle(item: CallHistoryItem): void {
    if (this.openId() === item.voice_id) {
      this.openId.set(null);
      this.detail.set(null);
      return;
    }
    this.open(item.voice_id);
  }

  /** Primer clic: pide confirmación durante 5 s. Segundo clic: borra. */
  askDelete(target: string): void {
    if (this.confirming() === target) {
      this.confirming.set(null);
      if (target === 'todo') this.deleteAll();
      else this.deleteOne(target);
      return;
    }
    this.confirming.set(target);
    if (this.confirmTimer) clearTimeout(this.confirmTimer);
    this.confirmTimer = setTimeout(() => this.confirming.set(null), 5000);
  }

  private deleteOne(voiceId: string): void {
    this.api
      .deleteCall(voiceId)
      .pipe(take(1))
      .subscribe({
        next: () => {
          this.seq++;                              // invalida un refresco que traiga la lista de antes
          this.items.set(this.items().filter((i) => i.voice_id !== voiceId));
          this.total.set(Math.max(0, this.total() - 1));
          if (this.openId() === voiceId) {
            this.openId.set(null);
            this.detail.set(null);
          }
        },
        error: (err: unknown) => this.error.set(err instanceof Error ? err.message : 'No pude borrarla.'),
      });
  }

  private deleteAll(): void {
    this.api
      .deleteAllCalls()
      .pipe(take(1))
      .subscribe({
        next: () => {
          this.openId.set(null);
          this.detail.set(null);
          this.items.set([]);
          this.total.set(0);
          this.loading.set(false);
          this.load(true);
        },
        error: (err: unknown) => this.error.set(err instanceof Error ? err.message : 'No pude borrar el historial.'),
      });
  }

  download(d: CallHistoryDetail): void {
    const cabecera = [
      `Llamada: ${d.contact_name || 'Sin ficha'}`,
      `Inicio: ${d.started}`,
      `Fin: ${d.ended ?? '-'}`,
      `Resultado: ${d.result ?? d.status}`,
      `Id: ${d.voice_id}`,
      '',
    ];
    const lineas = d.events.map((e) => {
      const extra = [
        e.tools?.length ? `  [herramientas: ${e.tools.join(', ')}]` : '',
        ...(e.warnings ?? []).map((w) => `  [aviso: ${w}]`),
      ].filter(Boolean);
      return [`${e.t.slice(11, 19)}  ${this.speaker(e.quien, d.contact_name)}: ${e.texto}`, ...extra].join('\n');
    });
    const blob = new Blob([[...cabecera, ...lineas].join('\n') + '\n'], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `llamada-${d.started.slice(0, 16).replace(/[:T]/g, '-')}-${d.voice_id.slice(0, 8)}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }

  speaker(quien: string, clinica?: string | null): string {
    if (quien === 'agente') return 'Asistente';
    if (quien === 'contraparte') return clinica || 'Clínica';
    return 'Sistema';
  }

  resultLabel(item: CallHistoryItem): string {
    if (item.status === 'en_curso') return 'en curso';
    const r = item.result ?? '';
    return (
      {
        appointment_booked: 'cita agendada',
        interview_or_role_confirmed: 'entrevista confirmada',
        rejected_or_unavailable: 'sin disponibilidad',
        voicemail_left: 'buzón de voz',
        callback_requested: 'devolver llamada',
        no_answer: 'no contestó',
        sin_consentimiento: 'sin consentimiento',
        sin_respuesta_al_aviso: 'sin respuesta al aviso',
        colgada: 'colgada',
        colgada_por_contraparte: 'colgó la clínica',
        silencio: 'silencio',
        error_api: 'error de la API',
      } as Record<string, string>
    )[r] ?? (r || 'terminada');
  }

  resultTone(item: CallHistoryItem): 'ok' | 'bad' | 'live' | '' {
    if (item.status === 'en_curso') return 'live';
    if (item.result === 'appointment_booked' || item.result === 'interview_or_role_confirmed') return 'ok';
    if (item.result === 'error_api' || item.result === 'rejected_or_unavailable') return 'bad';
    return '';
  }

  private open(voiceId: string, silent = false): void {
    this.openId.set(voiceId);
    if (!silent) {
      this.detail.set(null);
      this.detailLoading.set(true);
    }
    this.api
      .callDetail(voiceId)
      .pipe(take(1))
      .subscribe({
        next: (d) => {
          if (this.openId() === voiceId) this.detail.set(d);
          this.detailLoading.set(false);
        },
        error: (err: unknown) => {
          this.error.set(err instanceof Error ? err.message : 'No pude abrir la llamada.');
          this.detailLoading.set(false);
        },
      });
  }

  private load(reset: boolean, silent = false): void {
    if (this.loading() && silent) return;
    const mine = ++this.seq;
    const offset = reset ? 0 : this.items().length;
    if (!silent) this.loading.set(true);
    this.api
      .callHistory(PAGE, offset, this.query().trim())
      .pipe(take(1))
      .subscribe({
        next: (page) => {
          if (mine !== this.seq) return;           // respuesta vieja: ya hay una carga más nueva
          this.total.set(page.total);
          this.items.set(reset ? page.items : [...this.items(), ...page.items]);
          this.error.set(null);
          this.loading.set(false);
        },
        error: (err: unknown) => {
          if (mine !== this.seq) return;
          this.error.set(err instanceof Error ? err.message : 'No pude leer el historial.');
          this.loading.set(false);
        },
      });
  }
}
