import { actionNames, canPlayAction, type CharacterAction } from "./CharacterBehavior";
import type { DeskRole } from "./DeskScene";
import type { ScreenState } from "./ScreenSurface";

export interface CharacterReview {
  action: CharacterAction;
  playing: boolean;
  time: number;
  revision: number;
}
export type CharacterReviews = Partial<Record<DeskRole, CharacterReview>>;
export function reviewState(action: CharacterAction): ScreenState {
  return ["typing", "mouse", "drawing"].includes(action) ? "running" : "idle";
}
export function reviewActions(role: DeskRole) {
  return (Object.keys(actionNames) as CharacterAction[]).filter(action => canPlayAction(role, reviewState(action), action));
}
export function resolveReview(role: DeskRole, actualState: ScreenState, review?: CharacterReview) {
  if (!review || !canPlayAction(role, reviewState(review.action), review.action)) return { state: actualState, action: null };
  // Local visual review uses an appropriate simulated status; backend state is
  // never modified and automatic mode immediately returns to the real status.
  return { state: reviewState(review.action), action: review.action };
}
