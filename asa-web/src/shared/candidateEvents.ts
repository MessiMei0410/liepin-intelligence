export const CANDIDATE_UPDATED_EVENT = 'asa:candidate-updated'

export type CandidateUpdatedDetail = {
  id: number
  stage?: string
  isStopped?: boolean
}

export function dispatchCandidateUpdated(detail: CandidateUpdatedDetail) {
  window.dispatchEvent(new CustomEvent<CandidateUpdatedDetail>(CANDIDATE_UPDATED_EVENT, { detail }))
}
