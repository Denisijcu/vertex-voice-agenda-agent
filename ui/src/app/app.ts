import { ChangeDetectionStrategy, Component } from '@angular/core';
import { CallConsoleComponent } from './features/call-console/call-console.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CallConsoleComponent],
  template: `<app-call-console />`,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class App {}