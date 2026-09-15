import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AccessPanel } from "./AccessPanel";
import { ApiError, StudioApi } from "./api";
import { QuestionEditor } from "./QuestionEditor";
import type { Principal, Question, QuizSummary } from "./types";

const blankQuestion = (): Question => ({ type: "mcq", question: "", answers: ["", ""], correct: 0, time: 30, points: 1000 });
const clone = <T,>(value: T): T => structuredClone(value);

export function answerPreview(question: Question): string {
  if (["mcq", "fastest"].includes(question.type)) {
    const indexes = Array.isArray(question.correct) ? question.correct : [question.correct];
    return indexes.map((index) => question.answers?.[Number(index)]).filter(Boolean).join(", ");
  }
  if (question.type === "poll") return "Без правильного ответа";
  if (question.type === "crossword") return `${question.difficulty_levels?.length ?? 0} уровней`;
  return Array.isArray(question.correct) ? question.correct.join(", ") : String(question.correct ?? "");
}

function tokenFromLocation(): string {
  try { return decodeURIComponent(location.hash.slice(1)); } catch { return ""; }
}

export function App() {
  const attemptedAutomaticLogin = useRef(false);
  const [token, setToken] = useState(() => tokenFromLocation() || sessionStorage.getItem("quiz_editor_token") || "");
  const [tokenInput, setTokenInput] = useState(token);
  const [locked, setLocked] = useState(true);
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [revision, setRevision] = useState(0);
  const [quizzes, setQuizzes] = useState<QuizSummary[]>([]);
  const [quiz, setQuiz] = useState("");
  const [questions, setQuestions] = useState<Question[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [draft, setDraft] = useState<Question | null>(null);
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const [accessOpen, setAccessOpen] = useState(false);
  const [search, setSearch] = useState("");
  const api = useMemo(() => new StudioApi(token), [token]);
  const readOnly = principal?.role === "viewer";

  const toast = (text: string, error = false) => {
    setNotice({ text, error });
    window.setTimeout(() => setNotice((current) => current?.text === text ? null : current), 4000);
  };

  const fail = useCallback((error: unknown) => {
    const message = error instanceof Error ? error.message : "Неизвестная ошибка";
    toast(message, true);
    if (error instanceof ApiError && error.status === 401) {
      sessionStorage.removeItem("quiz_editor_token"); setLocked(true); setPrincipal(null);
    }
  }, []);

  const loadState = useCallback(async (client = api) => {
    const data = await client.state();
    setRevision(data.revision); setPrincipal(data.principal); setQuizzes(data.quizzes);
    return data;
  }, [api]);

  const unlock = async () => {
    const clean = tokenInput.trim();
    if (!clean) return;
    const client = new StudioApi(clean);
    try {
      const data = await loadState(client);
      setToken(clean); setPrincipal(data.principal); setLocked(false);
      sessionStorage.setItem("quiz_editor_token", clean);
      history.replaceState(null, "", `${location.pathname}${location.search}#${encodeURIComponent(clean)}`);
    } catch (error) { fail(error); }
  };

  useEffect(() => {
    if (!attemptedAutomaticLogin.current && tokenInput) {
      attemptedAutomaticLogin.current = true;
      void unlock();
    }
  }, []);

  const selectQuiz = async (name: string, preserve = false) => {
    try {
      const data = await api.quiz(name);
      setQuiz(data.name); setQuestions(data.questions); setRevision(data.revision);
      if (!preserve || selected === null || selected >= data.questions.length) { setSelected(null); setDraft(null); }
      else setDraft(clone(data.questions[selected]));
    } catch (error) { fail(error); }
  };

  const refresh = async () => {
    try { await loadState(); if (quiz) await selectQuiz(quiz, true); toast("Данные синхронизированы"); }
    catch (error) { fail(error); }
  };

  const saveQuestion = async (question: Question) => {
    try {
      const data = await api.saveQuestion(quiz, question, revision, selected);
      setQuestions(data.questions); setRevision(data.revision); setSelected(data.index); setDraft(clone(data.questions[data.index]));
      await loadState(); toast("Вопрос сохранён");
    } catch (error) { fail(error); throw error; }
  };

  const duplicateQuestion = async () => {
    if (selected === null) return;
    try {
      const data = await api.addQuestion(quiz, clone(questions[selected]), revision, selected + 1);
      setQuestions(data.questions); setRevision(data.revision); setSelected(data.index); setDraft(clone(data.questions[data.index])); toast("Создана копия вопроса");
    } catch (error) { fail(error); }
  };

  const deleteQuestion = async () => {
    if (selected === null || !confirm("Удалить этот вопрос?")) return;
    try { const data = await api.deleteQuestion(quiz, selected, revision); setQuestions(data.questions); setRevision(data.revision); setSelected(null); setDraft(null); await loadState(); toast("Вопрос удалён"); }
    catch (error) { fail(error); }
  };

  const moveQuestion = async (direction: -1 | 1) => {
    if (selected === null) return;
    try { const data = await api.moveQuestion(quiz, selected, selected + direction, revision); setQuestions(data.questions); setRevision(data.revision); setSelected(data.index); setDraft(clone(data.questions[data.index])); }
    catch (error) { fail(error); }
  };

  const createQuiz = async () => {
    const name = prompt("Название нового теста:")?.trim(); if (!name) return;
    try { const data = await api.createQuiz(name, revision); await loadState(); await selectQuiz(data.name); setSelected(null); setDraft(blankQuestion()); toast("Тест создан"); }
    catch (error) { fail(error); }
  };

  const renameQuiz = async () => {
    const name = prompt("Новое название теста:", quiz)?.trim(); if (!name || name === quiz) return;
    try { const data = await api.renameQuiz(quiz, name, revision); setQuiz(data.name); await loadState(); await selectQuiz(data.name, true); toast("Название изменено"); }
    catch (error) { fail(error); }
  };

  const deleteQuiz = async () => {
    if (!quiz || !confirm(`Удалить тест «${quiz}» целиком?`)) return;
    try { await api.deleteQuiz(quiz, revision); setQuiz(""); setQuestions([]); setDraft(null); setSelected(null); await loadState(); toast("Тест удалён"); }
    catch (error) { fail(error); }
  };

  const visibleQuizzes = quizzes.filter((item) => item.name.toLowerCase().includes(search.toLowerCase()));
  const completion = questions.length ? Math.round((questions.filter((q) => q.question && (q.type === "poll" || q.correct !== undefined || q.type === "crossword")).length / questions.length) * 100) : 0;

  if (locked) return <div className="auth-screen">
    <div className="glow glow-a" /><div className="glow glow-b" />
    <main className="auth-card">
      <div className="logo-mark">Q</div><div className="eyebrow">Quiz Studio · secure workspace</div>
      <h1>Создавайте игры,<br /><em>которые запоминают.</em></h1>
      <p>Введите персональный ключ. Если вам прислали готовую ссылку, он уже подставлен.</p>
      <label className="auth-input"><span>Ключ доступа</span><input autoFocus type="password" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void unlock()} placeholder="qz_••••••••••••••••" /></label>
      <button className="primary-button auth-submit" onClick={() => void unlock()}>Войти в студию <span>→</span></button>
      <small>Ключ остаётся только в этой вкладке браузера</small>
    </main>
  </div>;

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><div className="logo-mark small">Q</div><div><b>Quiz Studio</b><span>Interactive Suite</span></div></div>
      <nav><a href="/host.html">Запустить игру</a><a href="/admin.html">Live-пульт</a></nav>
      <div className="top-actions">
        <button className="round-button" onClick={() => void refresh()} title="Обновить">↻</button>
        {principal?.owner && <button className="team-button" onClick={() => setAccessOpen(true)}><span className="avatars">МК</span> Команда</button>}
        <div className="profile"><div className="avatar">{principal?.name.slice(0, 2).toUpperCase()}</div><div><b>{principal?.name}</b><span>{principal?.role === "owner" ? "Владелец" : principal?.role === "editor" ? "Редактор" : "Просмотр"}</span></div></div>
      </div>
    </header>

    <main className="studio-grid">
      <aside className="sidebar quizzes-panel">
        <div className="sidebar-head"><div><div className="eyebrow">Библиотека</div><h2>Мои тесты <span>{quizzes.length}</span></h2></div>{!readOnly && <button className="add-button" onClick={() => void createQuiz()}>+</button>}</div>
        <label className="search"><span>⌕</span><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Найти тест" /></label>
        <div className="sidebar-list">{visibleQuizzes.map((item, i) => <button key={item.name} className={`quiz-row ${quiz === item.name ? "active" : ""}`} onClick={() => void selectQuiz(item.name)}><span className={`quiz-icon tone-${i % 5}`}>◇</span><span><b>{item.name}</b><small>{item.question_count} вопросов</small></span><i>›</i></button>)}</div>
        <div className="storage-card"><div><b>Коллекция готова</b><span>{quizzes.reduce((sum, q) => sum + q.question_count, 0)} вопросов в {quizzes.length} тестах</span></div><div className="progress"><i style={{ width: `${Math.min(100, quizzes.length * 5)}%` }} /></div></div>
      </aside>

      <aside className="sidebar questions-panel">
        <div className="sidebar-head"><div><div className="eyebrow">Сценарий</div><h2>{quiz || "Выберите тест"}</h2></div>{quiz && !readOnly && <button className="add-button" onClick={() => { setSelected(null); setDraft(blankQuestion()); }}>+</button>}</div>
        {quiz && <div className="quiz-meta"><span>{questions.length} вопросов</span><span>Готовность {completion}%</span></div>}
        <div className="sidebar-list question-list">{questions.map((item, index) => <button key={index} className={`question-row ${selected === index ? "active" : ""}`} onClick={() => { setSelected(index); setDraft(clone(item)); }}><span className="question-number">{String(index + 1).padStart(2, "0")}</span><span><b>{item.question || "Без названия"}</b><small><i>{item.type}</i>{Number(item.time ?? 30)} сек · {Number(item.points ?? 0)} баллов</small><small className="answer">{answerPreview(item)}</small></span></button>)}</div>
        {quiz && <div className="quiz-actions">{!readOnly && <><button onClick={() => void renameQuiz()}>Переименовать</button><button className="danger-link" onClick={() => void deleteQuiz()}>Удалить</button></>}</div>}
      </aside>

      {draft && quiz ? <QuestionEditor key={`${quiz}:${selected ?? "new"}`} quiz={quiz} question={draft} index={selected} total={questions.length} readOnly={Boolean(readOnly)} onSave={saveQuestion} onDuplicate={duplicateQuestion} onDelete={deleteQuestion} onMove={moveQuestion} /> : <section className="workspace empty-workspace"><div className="empty-art"><span>Q</span></div><div className="eyebrow">Quiz Studio</div><h1>{quiz ? "Выберите вопрос" : "Выберите тест из библиотеки"}</h1><p>Здесь вы увидите содержимое, правильные ответы и все настройки игры.</p>{quiz && !readOnly && <button className="primary-button" onClick={() => { setSelected(null); setDraft(blankQuestion()); }}>Создать первый вопрос</button>}</section>}
    </main>

    {accessOpen && <AccessPanel onClose={() => setAccessOpen(false)} load={async () => { const [people, audit] = await Promise.all([api.collaborators(), api.audit()]); return { collaborators: people.collaborators, events: audit.events }; }} create={async (name, role) => (await api.createCollaborator(name, role)).token} revoke={async (id) => { await api.revokeCollaborator(id); }} />}
    {notice && <div className={`toast ${notice.error ? "error" : ""}`}><span>{notice.error ? "!" : "✓"}</span>{notice.text}</div>}
  </div>;
}
