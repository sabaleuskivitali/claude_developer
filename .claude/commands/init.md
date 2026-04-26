Выполни инициализацию сессии:

1. Запусти и выведи статус релизов по всем веткам:
```bash
echo "=== Не зарелижено ===" && \
echo "agent:  $(git log main..work/agent --oneline 2>/dev/null | wc -l | tr -d ' ') коммитов" && \
echo "server: $(git log main..work/server --oneline 2>/dev/null | wc -l | tr -d ' ') коммитов" && \
echo "cloud:  $(git log main..work/cloud --oneline 2>/dev/null | wc -l | tr -d ' ') коммитов"
```
Если у какой-то ветки > 0 — выведи предупреждение ⚠️ с именем ветки.

2. Проверь есть ли файл `.claude/session_status.md` в текущей папке.
   - Если есть — прочитай его и выведи: "📋 Предыдущая сессия: [Следующий шаг из файла]"
   - Если нет — пропусти.

3. Спроси: "Над чем работаем?" и выполни /rename Dev: [ответ].
