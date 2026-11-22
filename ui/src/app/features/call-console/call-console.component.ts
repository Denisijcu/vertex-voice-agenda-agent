import { JsonPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSelectModule } from '@angular/material/select';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { take } from 'rxjs';
import {
  CallPurpose,
  TranscriptLine,
  TurnExecutionResponse,
  VoiceTargetInput,
} from '../../core/models/call-agent.models';
import { CallAgentApiService } from '../../core/services/call-agent-api.service';
import { CallHistoryComponent } from '../call-history/call-history.component';
import { RealCallPanelComponent } from '../real-call/real-call-panel.component';

@Component({
  selector: 'app-call-console',
  standalone: true,
  imports: [
    FormsModule,
    JsonPipe,
    MatButtonModule,
    MatCardModule,
    MatChipsModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatProgressBarModule,
    MatSelectModule,
    MatSlideToggleModule,
    RealCallPanelComponent,
    CallHistoryComponent,
  ],
  templateUrl: './call-console.component.html',
  styleUrl: './call-console.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CallConsoleComponent {
  private readonly api = inject(CallAgentApiService);

  /** Vacío = el backend usa LLM_DEFAULT_MODEL del .env. */
  readonly modelName = signal<string>('vertex-voice-agenda');
  readonly contactName = signal<string>('Clínica Dental Coral Gables');
  readonly destination = signal<string>('sip:dental@pbx.local');
  readonly purpose = signal<CallPurpose>('dentist_appointment');
  readonly contextNotes = signal<string>(
    'Agendar una limpieza dental para Denis Sánchez. Paciente existente. Seguro: Delta Dental.'
  );
  readonly dryRun = signal<boolean>(true);
  readonly isLoading = signal<boolean>(false);
  readonly errorMessage = signal<string | null>(null);
  readonly lastTurn = signal<TurnExecutionResponse | null>(null);
  readonly activeCallId = signal<string | null>(null);
  readonly transcript = signal<TranscriptLine[]>([]);
  readonly callerUtterance = signal<string>('Sí, tenemos mañana a las 10 de la mañana. ¿Le sirve?');

  /** La misma ficha sirve para el simulador de texto y para la llamada real. */
  readonly target = computed<VoiceTargetInput>(() => ({
    contact_name: this.contactName().trim(),
    phone_or_sip_uri: this.destination().trim(),
    purpose: this.purpose(),
    context_notes: this.contextNotes().trim(),
    dry_run: this.dryRun(),
  }));

  readonly terminated = computed(() => this.lastTurn()?.terminated ?? false);
  readonly canStep = computed(
    () =>
      !this.isLoading() &&
      !this.terminated() &&
      (!this.activeCallId() || this.callerUtterance().trim().length > 0)
  );

  executeStep(): void {
    if (!this.canStep()) {
      return;
    }
    this.errorMessage.set(null);
    this.isLoading.set(true);

    const currentCallId = this.activeCallId();
    const utterance = currentCallId ? this.callerUtterance().trim() : null;

    this.api
      .stepCall({
        call_id: currentCallId,
        model_name: this.modelName().trim() || null,
        target: this.target(),
        caller_utterance: utterance,
        temperature: 0.2,
        max_tokens: 384,
      })
      .pipe(take(1))
      .subscribe({
        next: (res) => {
          const lines: TranscriptLine[] = [];
          if (utterance) {
            lines.push({ speaker: 'contraparte', text: utterance, tools: [] });
          }
          lines.push({
            speaker: 'agente',
            text: res.assistant_reply,
            tools: res.tool_calls.map((c) => c.tool),
          });
          this.transcript.update((t) => [...t, ...lines]);
          this.activeCallId.set(res.call_id);
          this.lastTurn.set(res);
          if (utterance) {
            this.callerUtterance.set('');
          }
          this.isLoading.set(false);
        },
        error: (err: unknown) => {
          this.errorMessage.set(
            err instanceof Error ? err.message : 'Error inesperado al ejecutar la llamada.'
          );
          this.isLoading.set(false);
        },
      });
  }

  resetCall(): void {
    this.activeCallId.set(null);
    this.lastTurn.set(null);
    this.errorMessage.set(null);
    this.transcript.set([]);
  }
}
