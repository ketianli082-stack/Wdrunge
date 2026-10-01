import identities from "../../backend/app/agents/identities.json";
import type { DeskRole } from "./DeskScene";

export const teamIdentities = identities;

export function memberName(id: string): string {
  if (id === "user") return "你";
  if (!Object.prototype.hasOwnProperty.call(identities, id)) return id;
  const member = identities[id as DeskRole];
  return `${member.name}（${member.role}）`;
}

// Older saved messages used role-only mentions. Keep their message text intact.
export function mentionPrefix(id: string, content: string): string | undefined {
  const identity = Object.prototype.hasOwnProperty.call(identities, id) ? identities[id as DeskRole] : undefined;
  return [memberName(id), identity?.role, identity?.name]
    .filter((name): name is string => !!name)
    .map(name => `@${name}`)
    .find(prefix => content.startsWith(prefix));
}
