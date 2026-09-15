import { useEffect, useMemo, useState } from "react";
import type { Question } from "./types";

const TYPES: Array<[Question["type"], string]> = [
  ["mcq", "Выбор ответа"], ["text", "Текст"], ["numeric", "Число"],
  ["wordle", "Wordle"], ["crossword", "Кроссворд"], ["poll", "Опрос"],
  ["fastest", "Кто быстрее"],
];

type Props = {
  quiz: string;
  question: Question;
  index: number | null;
  total: number;
  readOnly: boolean;
  onSave(question: Question): Promise<void>;
  onDuplicate(): Promise<void>;
  onDelete(): Promise<void>;
  onMove(direction: -1 | 1): Promise<void>;
};

export function QuestionEditor({ quiz, question, index, total, readOnly, onSave, onDuplicate, onDelete, onMove }: Props) {
  const [draft, setDraft] = useState<Question>(question);
  const [rawMode, setRawMode] = useState(question.type === "crossword");
  const [raw, setRaw] = useState(JSON.stringify(question, null, 2));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(structuredClone(question));
    setRaw(JSON.stringify(question, null, 2));
    setRawMode(question.type === "crossword");
  }, [question]);

  const choices = ["mcq", "fastest", "poll"].includes(draft.type);
  const correctText = useMemo(() => {
    if (["mcq", "fastest"].includes(draft.type)) {
      const indexes = Array.isArray(draft.correct) ? draft.correct : [draft.correct];
      return indexes.filter((v) => v !== undefined).map((v) => Number(v) + 1).join(", ");
    }
    return Array.isArray(draft.correct) ? draft.correct.join("\n") : String(draft.correct ?? "");
  }, [draft.correct, draft.type]);

  const update = <K extends keyof Question>(key: K, value: Question[K]) => setDraft((current) => ({ ...current, [key]: value }));
  const changeType = (type: Question["type"]) => {
    update("type", type);
    if (type === "crossword") setRawMode(true);
  };

  const submit = async () => {
    let result = structuredClone(draft);
    if (rawMode) {
      try { result = JSON.parse(raw) as Question; }
      catch (error) { throw new Error(`Ошибка JSON: ${(error as Error).message}`); }
    }
    setSaving(true);
    try { await onSave(result); } finally { setSaving(false); }
  };

  const setCorrect = (value: string) => {
    if (["mcq", "fastest"].includes(draft.type)) {
      const values = value.split(/[\s,;]+/).filter(Boolean).map((item) => Number(item) - 1);
      update("correct", values.length === 1 ? values[0] : values);
    } else {
      const values = value.split("\n").map((item) => item.trim()).filter(Boolean);
      update("correct", values.length === 1 ? values[0] : values);
    }
  };

  return <section className="workspace" aria-label="Редактор вопроса">
    <div className="workspace-head">
      <div>
        <div className="eyebrow">{index === null ? "Новый вопрос" : `Вопрос ${index + 1} из ${total}`}</div>
        <h2>{quiz}</h2>
      </div>
      <div className="status-chip"><span />{readOnly ? "Только просмотр" : "Черновик сохранён локально"}</div>
    </div>

    <div className="editor-scroll">
      <div className="question-card">
        <label className="field full"><span>Формулировка вопроса</span>
          <textarea className="hero-input" value={draft.question} disabled={readOnly} onChange={(e) => update("question", e.target.value)} placeholder="Что вы хотите спросить?" />
        </label>
        <div className="form-grid">
          <label className="field"><span>Формат</span><select value={draft.type} disabled={readOnly} onChange={(e) => changeType(e.target.value as Question["type"])}>{TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label className="field"><span>Подсказка</span><input value={String(draft.prompt ?? "")} disabled={readOnly} onChange={(e) => update("prompt", e.target.value)} placeholder="Необязательно" /></label>
          <label className="field"><span>Время</span><div className="input-unit"><input type="number" min="5" max="3600" value={Number(draft.time ?? 30)} disabled={readOnly} onChange={(e) => update("time", Number(e.target.value))} /><b>сек</b></div></label>
          <label className="field"><span>Награда</span><div className="input-unit"><input type="number" min="0" max="100000" value={Number(draft.points ?? 1000)} disabled={readOnly} onChange={(e) => update("points", Number(e.target.value))} /><b>баллов</b></div></label>
        </div>
      </div>

      {choices && <div className="question-card">
        <div className="section-title"><div><span className="step">02</span><h3>Варианты ответа</h3></div><small>Каждый вариант с новой строки</small></div>
        <textarea value={(draft.answers ?? []).join("\n")} disabled={readOnly} onChange={(e) => update("answers", e.target.value.split("\n"))} placeholder={"Первый вариант\nВторой вариант"} />
      </div>}

      {!(["poll", "crossword"].includes(draft.type)) && <div className="question-card">
        <div className="section-title"><div><span className="step">03</span><h3>Правильный ответ</h3></div><small>{choices ? "Номера вариантов: 1, 3" : "Допустимые ответы с новой строки"}</small></div>
        <textarea value={correctText} disabled={readOnly} onChange={(e) => setCorrect(e.target.value)} />
        {draft.type === "wordle" && <label className="field compact"><span>Количество попыток</span><input type="number" min="1" max="20" value={Number(draft.max_attempts ?? 6)} disabled={readOnly} onChange={(e) => update("max_attempts", Number(e.target.value))} /></label>}
      </div>}

      <div className="question-card">
        <div className="section-title"><div><span className="step">04</span><h3>Медиа</h3></div><small>Пути внутри /static</small></div>
        <div className="form-grid">
          <label className="field"><span>Изображение</span><input value={String(draft.image ?? "")} disabled={readOnly} onChange={(e) => update("image", e.target.value)} placeholder="/static/images/example.webp" /></label>
          <label className="field"><span>Аудио</span><input value={String(draft.audio ?? "")} disabled={readOnly} onChange={(e) => update("audio", e.target.value)} placeholder="/static/audios/example.mp3" /></label>
        </div>
      </div>

      <details className="advanced" open={draft.type === "crossword"}>
        <summary>Расширенный JSON <span>для кроссвордов и специальных настроек</span></summary>
        <label className="raw-toggle"><input type="checkbox" checked={rawMode} disabled={readOnly || draft.type === "crossword"} onChange={(e) => setRawMode(e.target.checked)} /> Сохранять JSON вместо полей формы</label>
        <textarea className="code" value={raw} disabled={readOnly} onChange={(e) => setRaw(e.target.value)} spellCheck={false} />
      </details>
    </div>

    {!readOnly && <div className="action-bar">
      <div className="action-cluster">
        <button className="icon-button" disabled={index === null || index <= 0} onClick={() => onMove(-1)} title="Переместить выше">↑</button>
        <button className="icon-button" disabled={index === null || index >= total - 1} onClick={() => onMove(1)} title="Переместить ниже">↓</button>
        <button className="ghost-button" disabled={index === null} onClick={onDuplicate}>Дублировать</button>
        <button className="danger-button" disabled={index === null} onClick={onDelete}>Удалить</button>
      </div>
      <button className="primary-button" disabled={saving} onClick={() => void submit()}>{saving ? "Сохраняю…" : "Сохранить вопрос"}<span>⌘↵</span></button>
    </div>}
  </section>;
}
