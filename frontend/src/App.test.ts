import { describe, expect, it } from "vitest";
import { answerPreview } from "./App";

describe("answerPreview", () => {
  it("resolves a multiple-choice answer", () => {
    expect(answerPreview({
      type: "mcq",
      question: "2 + 2?",
      answers: ["3", "4", "5"],
      correct: 1,
    })).toBe("4");
  });

  it("does not invent a correct answer for a poll", () => {
    expect(answerPreview({ type: "poll", question: "Ваш выбор?" })).toBe("Без правильного ответа");
  });
});
