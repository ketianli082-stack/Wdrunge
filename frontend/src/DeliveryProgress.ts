export type CollaborationProgressRequest = {
  conversation?: string;
  recipient?: string;
  state: string;
  created?: number;
  origin_message?: string;
  verification?: string;
  paused_on_restart?: boolean;
};

export function requestsForTurn(
  requests: CollaborationProgressRequest[],
  conversation: string | undefined,
  messageId: string | undefined,
  messageCreatedAt: number,
) {
  if (!conversation || !Number.isFinite(messageCreatedAt) || messageCreatedAt <= 0) return [];
  return requests.filter(request => request.conversation === conversation && (
    request.origin_message
      ? request.origin_message === messageId
      : (request.created || 0) * 1000 >= messageCreatedAt
  ));
}

export function reviewProgress(
  requests: CollaborationProgressRequest[],
  activityRunning: boolean,
) {
  const qualityRequests = requests.filter(request => request.recipient === 'testing' || request.verification === 'end_to_end');
  // A completed browser/tool call is not an accepted verification task. Require
  // the current batch and its verification task to be formally accepted; a
  // failed, cancelled, missing or merely delivered task cannot establish Ready.
  const accepted = qualityRequests.length > 0 && requests.every(request => request.state === 'completed' && !request.paused_on_restart);
  const checking = !accepted && (activityRunning || requests.some(request => ['queued', 'running', 'blocked', 'review'].includes(request.state)));
  return { checking, accepted };
}
