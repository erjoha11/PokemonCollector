import type { ClaimMatchAnswer, LotOptions } from "../domain/bids";
import { bidAnswerKey, claimMatchAnswerKey, claimPhotoAnswerKey, lotNameAnswerKey, type ClaimPhotoAnswer } from "./prompts";

// Claude's cached answers, as interpretLots asks for them: the overview, the watcher and retention
// all read them the same way.

export type AnswerLookups = Pick<LotOptions, "claimPhoto" | "claimMatch" | "lotName" | "answer">;

export function answerLookups(answers: Map<string, unknown>): AnswerLookups {
  return {
    claimPhoto: (input) => (answers.get(claimPhotoAnswerKey(input)) as ClaimPhotoAnswer | undefined)?.cards,
    claimMatch: (input) => answers.get(claimMatchAnswerKey(input)) as ClaimMatchAnswer | undefined,
    lotName: (imageUrl, text) => answers.get(lotNameAnswerKey(imageUrl, text)) as string | null | undefined,
    answer: (seller, text) => {
      const key = bidAnswerKey(seller, text);
      return answers.has(key) ? (answers.get(key) as number | null) : undefined;
    },
  };
}
