## Qué cambia

<!-- Una frase. Un cambio por PR. -->

## Por qué

<!-- La llamada, el log o el problema que lo motivó. Si viene de una llamada real, pega la parte de la transcripción. -->

## Cómo lo probé

- [ ] `api`: `.\venv\Scripts\python.exe -m unittest discover -s tests -t . -v` (desde `api`)
- [ ] `telephony`: `.\venv\Scripts\python.exe -m unittest discover -s tests -t . -v` (desde `telephony`)
- [ ] `ui`: `npx ng build` (desde `ui`)
- [ ] Llamada real de prueba (si toca la pasarela o el orquestador)

## Revisado

- [ ] No sube `libro/`, ningún `.env`, logs de llamadas (`telephony/logs`, `api/logs`, `api/data`) ni modelos.
- [ ] Si cambia una regla del orquestador, tiene su prueba con la llamada que la enseñó.
