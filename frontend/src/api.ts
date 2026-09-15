import type { AuditEvent, Collaborator, Principal, Question, QuizSummary } from "./types";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export class StudioApi {
  constructor(private readonly token: string) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...init, headers, cache: "no-store" });
    const payload = (await response.json().catch(() => ({}))) as { detail?: string };
    if (!response.ok) throw new ApiError(payload.detail || `Ошибка HTTP ${response.status}`, response.status);
    return payload as T;
  }

  state() {
    return this.request<{ revision: number; principal: Principal; quizzes: QuizSummary[] }>("/api/editor/state");
  }

  quiz(name: string) {
    return this.request<{ revision: number; name: string; questions: Question[] }>(`/api/editor/quiz?name=${encodeURIComponent(name)}`);
  }

  createQuiz(name: string, revision: number) {
    return this.request<{ revision: number; name: string }>("/api/editor/quiz", { method: "POST", body: JSON.stringify({ name, revision }) });
  }

  renameQuiz(current_name: string, name: string, revision: number) {
    return this.request<{ revision: number; name: string }>("/api/editor/quiz", { method: "PATCH", body: JSON.stringify({ current_name, name, revision }) });
  }

  deleteQuiz(name: string, revision: number) {
    return this.request<{ revision: number }>(`/api/editor/quiz?name=${encodeURIComponent(name)}&revision=${revision}`, { method: "DELETE" });
  }

  saveQuestion(quiz: string, question: Question, revision: number, index: number | null) {
    return this.request<{ revision: number; questions: Question[]; index: number }>("/api/editor/question", {
      method: index === null ? "POST" : "PUT",
      body: JSON.stringify({ quiz, question, revision, ...(index === null ? {} : { index }) }),
    });
  }

  addQuestion(quiz: string, question: Question, revision: number, index?: number) {
    return this.request<{ revision: number; questions: Question[]; index: number }>("/api/editor/question", {
      method: "POST", body: JSON.stringify({ quiz, question, revision, index }),
    });
  }

  deleteQuestion(quiz: string, index: number, revision: number) {
    return this.request<{ revision: number; questions: Question[] }>(`/api/editor/question?quiz=${encodeURIComponent(quiz)}&index=${index}&revision=${revision}`, { method: "DELETE" });
  }

  moveQuestion(quiz: string, index: number, target_index: number, revision: number) {
    return this.request<{ revision: number; questions: Question[]; index: number }>("/api/editor/question/move", { method: "POST", body: JSON.stringify({ quiz, index, target_index, revision }) });
  }

  collaborators() { return this.request<{ collaborators: Collaborator[] }>("/api/editor/collaborators"); }
  createCollaborator(name: string, role: "editor" | "viewer") {
    return this.request<{ collaborator: Collaborator; token: string }>("/api/editor/collaborators", { method: "POST", body: JSON.stringify({ name, role }) });
  }
  revokeCollaborator(id: string) { return this.request<{ ok: boolean }>(`/api/editor/collaborators/${id}`, { method: "DELETE" }); }
  audit() { return this.request<{ events: AuditEvent[] }>("/api/editor/audit?limit=100"); }
}
