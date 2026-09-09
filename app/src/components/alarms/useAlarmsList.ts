import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { alertsInboxService, type InboxPage } from '../../services/alertsInboxService'

/** Every query key an ack should invalidate, wherever it was fired. */
const ALARM_KEYS = ['alarms-page', 'vehicle-alarms', 'alerts-inbox-unacked']

export function useAlarmsList(params: {
  queryKeyPrefix: string
  sourceName?: string
  unacked?: boolean
  severity?: string | null
  page: number
  pageSize: number
  skip: number
}) {
  const { queryKeyPrefix, sourceName, unacked, severity, page, pageSize, skip } = params
  const query = useQuery({
    queryKey: [queryKeyPrefix, sourceName, unacked, severity, page, pageSize],
    queryFn: async () => {
      const { data } = await alertsInboxService.listInboxAlerts({
        source_name: sourceName,
        unacked: unacked || undefined,
        severity: severity || undefined,
        skip,
        limit: pageSize,
      })
      return data as InboxPage
    },
    // Page 1 is the live inbox and must keep polling. Deeper pages are
    // someone reading back through history, and the list is ordered
    // newest-first — so every alarm that arrives while they read shifts
    // every row down by one, under the cursor. Refreshing there is not a
    // feature, it is the page moving while you look at it.
    refetchInterval: page === 1 ? 10_000 : false,
    // Otherwise the table empties on every page change and every poll.
    placeholderData: keepPreviousData,
  })

  return {
    rows: query.data?.alerts ?? [],
    total: query.data?.total,
    unackedCount: query.data?.unacked_count ?? 0,
    isPending: query.isPending,
    isFetching: query.isFetching,
    isError: query.isError,
    error: query.error,
    refetch: query.refetch,
  }
}

export function useAckAlarms(onDone?: () => void) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (target: number[] | { source_name?: string; severity?: string }) =>
      Array.isArray(target)
        ? alertsInboxService.ackInboxAlerts(target)
        : alertsInboxService.ackInboxAlertsMatching(target),
    onSuccess: () => {
      // Every alarm surface, not just the one that fired the ack. These
      // used to be invalidated asymmetrically, so acking on one page left
      // the other showing the alarm as live until its own poll came round.
      ALARM_KEYS.forEach((key) => queryClient.invalidateQueries({ queryKey: [key] }))
      onDone?.()
    },
  })
}
