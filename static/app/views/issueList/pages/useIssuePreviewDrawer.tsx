import {useCallback, useEffect, useRef} from 'react';
import {parseAsString, useQueryState} from 'nuqs';

import {useDrawer} from '@sentry/scraps/drawer';

import {t} from 'sentry/locale';
import type {Group} from 'sentry/types/group';
import {IssuePreviewDrawer} from 'sentry/views/issueDetails/issuePreview/issuePreviewDrawer';

/**
 * Query param holding the id of the issue whose preview drawer is open.
 * Presence opens the drawer; absence closes it.
 */
export const SELECTED_ISSUE_QUERY_PARAM = 'preview';

interface UseIssuePreviewDrawerOptions {
  enabled?: boolean;
  /** Ordered list of visible group IDs — used to compute prev/next navigation. */
  groupIds?: string[];
}

/**
 * Opens a lightweight issue preview drawer.
 * The open/selected issue state is stored in the `preview` query param.
 */
export function useIssuePreviewDrawer({
  enabled = true,
  groupIds = [],
}: UseIssuePreviewDrawerOptions = {}) {
  const {openDrawer} = useDrawer();

  const [selectedIssueId, setSelectedIssueId] = useQueryState(
    SELECTED_ISSUE_QUERY_PARAM,
    parseAsString.withOptions({history: 'replace'})
  );

  const openIssuePreview = useCallback(
    (group: Group) => {
      setSelectedIssueId(group.id);
    },
    [setSelectedIssueId]
  );

  // Keep a stable ref so the openDrawer render function always sees the latest list
  const groupIdsRef = useRef(groupIds);
  groupIdsRef.current = groupIds;

  // Persist the active tab across issue navigations so switching issues doesn't reset it
  const activeTabRef = useRef('activity');

  const lastOpenedIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled || !selectedIssueId) {
      lastOpenedIdRef.current = null;
      return;
    }

    if (lastOpenedIdRef.current === selectedIssueId) {
      return;
    }

    lastOpenedIdRef.current = selectedIssueId;

    const currentIndex = groupIdsRef.current.indexOf(selectedIssueId);
    const prevId = currentIndex > 0 ? groupIdsRef.current[currentIndex - 1] : undefined;
    const nextId =
      currentIndex < groupIdsRef.current.length - 1
        ? groupIdsRef.current[currentIndex + 1]
        : undefined;

    openDrawer(
      () => (
        <IssuePreviewDrawer
          groupId={selectedIssueId}
          onNavigatePrev={prevId ? () => setSelectedIssueId(prevId) : undefined}
          onNavigateNext={nextId ? () => setSelectedIssueId(nextId) : undefined}
          activeTab={activeTabRef.current}
          onTabChange={(tab: string) => {
            activeTabRef.current = tab;
          }}
        />
      ),
      {
        ariaLabel: t('Issue preview'),
        drawerKey: 'issue-preview-drawer',
        mode: 'passive',
        shouldCloseOnLocationChange: nextLocation =>
          !nextLocation.query[SELECTED_ISSUE_QUERY_PARAM],
        onClose: () => setSelectedIssueId(null),
      }
    );
  }, [enabled, selectedIssueId, openDrawer, setSelectedIssueId]);

  return {openIssuePreview, selectedIssueId};
}
