export type Role = "owner" | "editor" | "viewer";

export type Principal = {
  id: string;
  name: string;
  role: Role;
  owner: boolean;
};

export type Question = {
  type: "mcq" | "numeric" | "text" | "wordle" | "crossword" | "poll" | "fastest";
  question: string;
  time?: number;
  points?: number;
  prompt?: string;
  answers?: string[];
  correct?: string | number | Array<string | number>;
  max_attempts?: number;
  image?: string;
  audio?: string;
  difficulty_levels?: unknown[];
  [key: string]: unknown;
};

export type QuizSummary = { name: string; question_count: number };

export type Collaborator = {
  id: string;
  name: string;
  role: "editor" | "viewer";
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};

export type AuditEvent = {
  id: number;
  created_at: string;
  actor_name: string;
  action: string;
  target: string;
  details: string;
};
