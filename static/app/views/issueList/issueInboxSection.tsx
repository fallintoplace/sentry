import {useEffect, useMemo, useRef, useState} from 'react';
import styled from '@emotion/styled';
import {useQuery} from '@tanstack/react-query';

import type {CursorHandler} from '@sentry/scraps/pagination';
import {getPaginationCaption, Pagination} from '@sentry/scraps/pagination';

import {ActorAvatar} from '@sentry/scraps/avatar';
import {LinkButton} from '@sentry/scraps/button';
import {Checkbox} from '@sentry/scraps/checkbox';
import {Flex} from '@sentry/scraps/layout';
import {Text} from '@sentry/scraps/text';
import {Tooltip} from '@sentry/scraps/tooltip';

import {bulkDelete, mergeGroups} from 'sentry/actionCreators/group';
import {ErrorBoundary} from 'sentry/components/errorBoundary';
import {ErrorLevel} from 'sentry/components/events/errorLevel';
import ProjectBadge from 'sentry/components/idBadge/projectBadge';
import {LoadingError} from 'sentry/components/loadingError';
import {LoadingIndicator} from 'sentry/components/loadingIndicator';
import {usePageFilters} from 'sentry/components/pageFilters/usePageFilters';
import {TimeSince} from 'sentry/components/timeSince';
import {IconOpen} from 'sentry/icons';
import {t, tct} from 'sentry/locale';
import {GroupStore} from 'sentry/stores/groupStore';
import type {Group} from 'sentry/types/group';
import {apiOptions, selectJsonWithHeaders} from 'sentry/utils/api/apiOptions';
import {uniq} from 'sentry/utils/array/uniq';
import {getMessage, getTitle} from 'sentry/utils/events';
import {normalizeUrl} from 'sentry/utils/url/normalizeUrl';
import {useApi} from 'sentry/utils/useApi';
import {useOrganization} from 'sentry/utils/useOrganization';
import {useProjects} from 'sentry/utils/useProjects';
import {useSyncedLocalStorageState} from 'sentry/utils/useSyncedLocalStorageState';
import {GroupDataContextProvider} from 'sentry/views/issueDetails/groupDataContext';
import {IssueIdBreadcrumb} from 'sentry/views/issueDetails/header/issueIdBreadcrumb';
import {IssuePreviewContent} from 'sentry/views/issueDetails/issuePreview/issuePreviewDrawer';
import {useGroup} from 'sentry/views/issueDetails/useGroup';
import {ActionSet} from 'sentry/views/issueList/actions/actionSet';
import {performBulkUpdate} from 'sentry/views/issueList/actions/utils';
import {
  IssueSelectionProvider,
  useIssueSelectionActions,
  useIssueSelectionSummary,
  useOptionalIssueSelectionActions,
  useOptionalIssueSelectionSummary,
} from 'sentry/views/issueList/issueSelectionContext';
import type {IssueUpdateData} from 'sentry/views/issueList/types';
import {useIssueProgress} from 'sentry/views/issueList/useIssueProgress';
import {getProgressIcon} from 'sentry/views/issueList/utils/progress';

interface IssueInboxSectionProps {
  query: string;
  onActionTaken?: () => void;
  sort?: string;
}

const MIN_LIST_WIDTH = 240;
const MAX_LIST_WIDTH = 600;
const DEFAULT_LIST_WIDTH = 360;

export function IssueInboxSection({query, sort, onActionTaken}: IssueInboxSectionProps) {
  const organization = useOrganization();
  const {selection} = usePageFilters();
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null);
  const [inboxActiveTab, setInboxActiveTab] = useState('activity');
  const [listWidth, setListWidth] = useSyncedLocalStorageState(
    'issue-inbox-list-width',
    DEFAULT_LIST_WIDTH
  );
  const [cursor, setCursor] = useState<string | undefined>(undefined);
  const [pageIndex, setPageIndex] = useState(0);
  const layoutRef = useRef<HTMLDivElement>(null);

  const {data, isPending, isError, refetch} = useQuery({
    ...apiOptions.as<Group[]>()('/organizations/$organizationIdOrSlug/issues/', {
      path: {organizationIdOrSlug: organization.slug},
      query: {
        query,
        sort,
        cursor,
        project: selection.projects,
        environment: selection.environments,
        expand: ['inbox', 'owners'],
        limit: 25,
      },
      staleTime: 30_000,
    }),
    select: selectJsonWithHeaders,
  });

  const groups = useMemo(() => data?.json ?? [], [data]);
  const pageLinks = data?.headers.Link;
  const totalHits = data?.headers['X-Hits'];

  useEffect(() => {
    if (groups.length > 0) {
      GroupStore.add(groups);
      if (!selectedGroupId) {
        setSelectedGroupId(groups[0]!.id);
      }
    }
  }, [groups, selectedGroupId]);

  const groupIds = groups.map(g => g.id);

  function handleRefetch() {
    refetch();
    onActionTaken?.();
  }

  function handleResizeMouseDown(e: React.MouseEvent) {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = listWidth;

    function onMouseMove(moveEvent: MouseEvent) {
      const delta = moveEvent.clientX - startX;
      const next = Math.min(MAX_LIST_WIDTH, Math.max(MIN_LIST_WIDTH, startWidth + delta));
      setListWidth(next);
    }

    function onMouseUp() {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }

    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  }

  return (
    <IssueSelectionProvider visibleGroupIds={groupIds}>
      <InboxLayout ref={layoutRef}>
        <InboxLeftPanel style={{width: listWidth, minWidth: listWidth}}>
          <InboxActionsHeader
            groupIds={groupIds}
            query={query}
            selection={selection}
            onDelete={handleRefetch}
            onActionTaken={handleRefetch}
            pageLinks={pageLinks}
            onCursor={(nextCursor, _path, _query, delta) => {
              setCursor(nextCursor);
              setPageIndex(p => p + delta);
            }}
            pageIndex={pageIndex}
            totalHits={totalHits}
          />
          <InboxList>
            {isPending && <LoadingIndicator />}
            {isError && <LoadingError />}
            {groups.map(group => (
              <InboxListItem
                key={group.id}
                group={group}
                isSelected={group.id === selectedGroupId}
                onClick={() => setSelectedGroupId(group.id)}
              />
            ))}
          </InboxList>
        </InboxLeftPanel>
        <ResizeHandle
          onMouseDown={handleResizeMouseDown}
          role="separator"
          aria-label={t('Resize inbox panel')}
          aria-orientation="vertical"
        />
        <InboxDetailPanel>
          {selectedGroupId ? (
            <InboxDetail
              groupId={selectedGroupId}
              activeTab={inboxActiveTab}
              onTabChange={setInboxActiveTab}
            />
          ) : (
            <Flex align="center" justify="center" style={{height: '100%'}}>
              <Text variant="muted">{t('Select an issue to view details')}</Text>
            </Flex>
          )}
        </InboxDetailPanel>
      </InboxLayout>
    </IssueSelectionProvider>
  );
}

interface InboxActionsHeaderProps {
  groupIds: string[];
  onActionTaken: () => void;
  onCursor: CursorHandler;
  onDelete: () => void;
  query: string;
  selection: ReturnType<typeof usePageFilters>['selection'];
  pageIndex?: number;
  pageLinks?: string;
  totalHits?: number;
}

function InboxActionsHeader({
  groupIds,
  query,
  selection,
  onDelete,
  onActionTaken,
  pageLinks,
  onCursor,
  pageIndex = 0,
  totalHits,
}: InboxActionsHeaderProps) {
  const api = useApi();
  const organization = useOrganization();
  const {setAllInQuerySelected, deselectAll, toggleSelectAllVisible} =
    useIssueSelectionActions();
  const {pageSelected, multiSelected, anySelected, allInQuerySelected, selectedIdsSet} =
    useIssueSelectionSummary();
  const queryCount = groupIds.length;

  const selectedProjectSlug = useMemo(() => {
    const projects = [...selectedIdsSet]
      .map(id => GroupStore.get(id))
      .filter((group): group is Group => !!group?.project)
      .map(group => group.project.slug);
    const uniqProjects = uniq(projects);
    return uniqProjects.length === 1 ? uniqProjects[0] : undefined;
  }, [selectedIdsSet]);

  const queryExcludingPerformanceIssues = `${query ?? ''} issue.category:error`;

  function actionSelectedGroups(callback: (itemIds: string[] | undefined) => void) {
    const selectedIds = allInQuerySelected
      ? undefined
      : groupIds.filter(itemId => selectedIdsSet.has(itemId));
    callback(selectedIds);
    deselectAll();
  }

  function handleDelete() {
    actionSelectedGroups(itemIds => {
      bulkDelete(
        api,
        {
          orgId: organization.slug,
          itemIds,
          query: queryExcludingPerformanceIssues,
          project: selection.projects,
          environment: selection.environments,
          ...selection.datetime,
        },
        {complete: onDelete}
      );
    });
  }

  function handleMerge() {
    actionSelectedGroups(itemIds => {
      mergeGroups(api, {
        orgId: organization.slug,
        itemIds,
        query: queryExcludingPerformanceIssues,
        project: selection.projects,
        environment: selection.environments,
        ...selection.datetime,
      });
    });
  }

  function handleUpdate(data: IssueUpdateData) {
    actionSelectedGroups(itemIds => {
      performBulkUpdate({
        api,
        data,
        itemIds,
        organizationSlug: organization.slug,
        query: queryExcludingPerformanceIssues,
        selection,
        onSuccess: () => onActionTaken(),
      });
    });
  }

  return (
    <ActionsBarContainer>
      <Checkbox
        aria-label={t('Select all issues')}
        checked={anySelected && !pageSelected ? 'indeterminate' : pageSelected}
        onChange={() => toggleSelectAllVisible()}
        size="sm"
      />
      {anySelected ? (
        <ActionSet
          queryCount={queryCount}
          query={query}
          allInQuerySelected={allInQuerySelected}
          anySelected={anySelected}
          multiSelected={multiSelected}
          issues={selectedIdsSet}
          onShouldConfirm={() => selectedIdsSet.size > 1}
          onDelete={handleDelete}
          onMerge={handleMerge}
          onUpdate={handleUpdate}
          selectedProjectSlug={selectedProjectSlug}
        />
      ) : (
        <HeaderLabel>{t('Issues')}</HeaderLabel>
      )}
      {anySelected && (
        <SelectAllInfo>
          {allInQuerySelected
            ? t('All issues matching this search selected')
            : tct('[count] selected', {count: selectedIdsSet.size})}
          {!allInQuerySelected && (
            <SelectAllLink onClick={() => setAllInQuerySelected(true)}>
              {tct('Select all [count] issues', {count: queryCount})}
            </SelectAllLink>
          )}
        </SelectAllInfo>
      )}
      {pageLinks && totalHits !== undefined && (
        <InboxPagination
          pageLinks={pageLinks}
          onCursor={onCursor}
          caption={getPaginationCaption({
            cursor: `0:${pageIndex}:0`,
            limit: 25,
            pageLength: groupIds.length,
            total: totalHits,
          })}
          size="xs"
        />
      )}
    </ActionsBarContainer>
  );
}

interface InboxListItemProps {
  group: Group;
  isSelected: boolean;
  onClick: () => void;
}

function InboxListItem({group, isSelected, onClick}: InboxListItemProps) {
  const {title} = getTitle(group);
  const message = getMessage(group);
  const issueSelectionActions = useOptionalIssueSelectionActions();
  const issueSelectionSummary = useOptionalIssueSelectionSummary();
  const isChecked = issueSelectionSummary?.selectedIdsSet.has(group.id) ?? false;

  const {data: progressData} = useIssueProgress([group.id]);
  const progressState = progressData?.results[group.id]?.progress ?? null;

  return (
    <ListItemRoot isSelected={isSelected} onClick={onClick} role="button" tabIndex={0}>
      <ListItemLeft>
        <CheckboxWrapper
          onClick={e => {
            e.stopPropagation();
            issueSelectionActions?.toggleSelect(group.id);
          }}
        >
          <Checkbox
            aria-label={t('Select Issue')}
            checked={isChecked}
            onChange={() => {}}
            size="sm"
          />
        </CheckboxWrapper>
        <ListItemBody>
          <TopRow>
            <TitleArea>
              {!group.hasSeen && (
                <Tooltip title={t('Unread')} skipWrapper>
                  <UnreadDot />
                </Tooltip>
              )}
              <TitleText isRead={group.hasSeen}>{title}</TitleText>
            </TitleArea>
            <Timestamps>
              <TimeSince
                date={group.lastSeen}
                unitStyle="extraShort"
                suffix=""
                tooltipPrefix={t('Last seen')}
              />
              <TimestampDivider />
              <TimeSince
                date={group.firstSeen}
                unitStyle="extraShort"
                suffix=""
                tooltipPrefix={t('First seen')}
              />
            </Timestamps>
          </TopRow>

          <MiddleRow>
            <ErrorLevel level={group.level} />
            <SubtitleText muted={!message}>
              {message ?? t('No error message')}
            </SubtitleText>
          </MiddleRow>

          <BottomRow>
            <BottomLeft>
              <ProjectBadge project={group.project} avatarSize={12} disableLink />
            </BottomLeft>
            <BottomRight>
              {progressState !== null && progressState !== undefined && (
                <ProgressWrapper>{getProgressIcon(progressState)}</ProgressWrapper>
              )}
              {group.assignedTo && (
                <ActorAvatar actor={group.assignedTo} size={16} hasTooltip />
              )}
            </BottomRight>
          </BottomRow>
        </ListItemBody>
      </ListItemLeft>
    </ListItemRoot>
  );
}

interface InboxDetailProps {
  groupId: string;
  activeTab?: string;
  onTabChange?: (tab: string) => void;
}

function InboxDetail({groupId, activeTab, onTabChange}: InboxDetailProps) {
  const organization = useOrganization();
  const {data: group, isPending, isError} = useGroup({groupId});
  const {projects} = useProjects();
  const project = projects.find(p => p.id === group?.project.id) ?? group?.project;

  const issueDetailsUrl = normalizeUrl(
    `/organizations/${organization.slug}/issues/${groupId}/`
  );

  if (isPending) {
    return <LoadingIndicator />;
  }

  if (isError) {
    return <LoadingError />;
  }

  if (!group || !project) {
    return null;
  }

  return (
    <DetailRoot>
      <DetailHeader>
        <IssueIdBreadcrumb group={group} project={project} />
        <LinkButton to={issueDetailsUrl} size="xs" icon={<IconOpen />}>
          {t('Open Issue')}
        </LinkButton>
      </DetailHeader>
      <DetailBody>
        <GroupDataContextProvider group={group} project={project}>
          <ErrorBoundary mini>
            <IssuePreviewContent
              fullWidthTabs
              activeTab={activeTab}
              onTabChange={onTabChange}
            />
          </ErrorBoundary>
        </GroupDataContextProvider>
      </DetailBody>
    </DetailRoot>
  );
}

const InboxLayout = styled('div')`
  display: flex;
  flex-direction: row;
  flex: 1;
  min-height: 0;
  border-top: 1px solid ${p => p.theme.tokens.border.primary};
  overflow: hidden;
`;

const InboxLeftPanel = styled('div')`
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
`;

const ResizeHandle = styled('div')`
  width: 1px;
  flex-shrink: 0;
  cursor: col-resize;
  position: relative;
  border-left: 1px solid ${p => p.theme.tokens.border.primary};
  transition: border-color 0.15s;
  z-index: 1;

  /* Widen the interactive hit area without widening the visual line */
  &::after {
    content: '';
    position: absolute;
    inset: 0 -4px;
  }

  &:hover,
  &:active {
    border-left-color: ${p => p.theme.tokens.border.accent};
  }
`;

const InboxList = styled('div')`
  flex: 1;
  overflow-y: auto;
  scrollbar-width: none;
  background: ${p => p.theme.tokens.background.primary};

  &::-webkit-scrollbar {
    display: none;
  }
`;

const InboxDetailPanel = styled('div')`
  flex: 1;
  min-width: 0;
  overflow: hidden;
  background: ${p => p.theme.tokens.background.primary};
  display: flex;
  flex-direction: column;
`;

const InboxPagination = styled(Pagination)`
  margin: 0 0 0 auto;
`;

const ActionsBarContainer = styled('div')`
  display: flex;
  align-items: center;
  gap: ${p => p.theme.space.sm};
  padding: ${p => p.theme.space.xs} ${p => p.theme.space.md};
  border-bottom: 1px solid ${p => p.theme.tokens.border.primary};
  background: ${p => p.theme.tokens.background.secondary};
  flex-shrink: 0;
  min-height: 36px;
`;

const HeaderLabel = styled('span')`
  font-size: ${p => p.theme.font.size.sm};
  font-weight: ${p => p.theme.font.weight.sans.medium};
  color: ${p => p.theme.tokens.content.primary};
`;

const SelectAllInfo = styled('span')`
  font-size: ${p => p.theme.font.size.sm};
  color: ${p => p.theme.tokens.content.secondary};
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: ${p => p.theme.space.xs};
`;

const SelectAllLink = styled('a')`
  cursor: pointer;
  color: ${p => p.theme.tokens.content.accent};
  &:hover {
    text-decoration: underline;
  }
`;

interface ListItemRootProps {
  isSelected: boolean;
}

const ListItemRoot = styled('div')<ListItemRootProps>`
  display: flex;
  align-items: stretch;
  padding: ${p => p.theme.space.md} ${p => p.theme.space.md};
  cursor: pointer;
  border-bottom: 1px solid ${p => p.theme.tokens.border.secondary};
  background: ${p =>
    p.isSelected ? p.theme.tokens.background.secondary : 'transparent'};
  border-left: 3px solid
    ${p => (p.isSelected ? p.theme.tokens.border.accent : 'transparent')};
  position: relative;

  &:hover {
    background: ${p => p.theme.tokens.background.secondary};
  }
`;

const ListItemLeft = styled('div')`
  display: flex;
  align-items: flex-start;
  gap: ${p => p.theme.space.sm};
  width: 100%;
  min-width: 0;
`;

const CheckboxWrapper = styled('div')`
  flex-shrink: 0;
  padding-top: 2px;
`;

const ListItemBody = styled('div')`
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: ${p => p.theme.space.xs};
`;

const TopRow = styled('div')`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: ${p => p.theme.space.sm};
  min-width: 0;
`;

const TitleArea = styled('div')`
  display: flex;
  align-items: center;
  gap: ${p => p.theme.space.xs};
  min-width: 0;
  flex: 1;
`;

const UnreadDot = styled('div')`
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: ${p => p.theme.tokens.graphics.accent.vibrant};
`;

const TitleText = styled('span')<{isRead: boolean}>`
  font-size: ${p => p.theme.font.size.md};
  font-weight: ${p => p.theme.font.weight.sans.medium};
  color: ${p =>
    p.isRead ? p.theme.tokens.content.secondary : p.theme.tokens.content.primary};
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
`;

const Timestamps = styled('div')`
  display: flex;
  align-items: center;
  gap: ${p => p.theme.space.xs};
  flex-shrink: 0;
  font-size: ${p => p.theme.font.size.xs};
  color: ${p => p.theme.tokens.content.secondary};
`;

const TimestampDivider = styled('span')`
  width: 1px;
  height: 10px;
  /* eslint-disable @sentry/scraps/use-semantic-token */
  background: ${p => p.theme.tokens.border.secondary};
  /* eslint-enable @sentry/scraps/use-semantic-token */
`;

const MiddleRow = styled('div')`
  display: flex;
  align-items: center;
  gap: ${p => p.theme.space.xs};
  min-width: 0;
`;

const SubtitleText = styled('span')<{muted?: boolean}>`
  font-size: ${p => p.theme.font.size.sm};
  color: ${p =>
    p.muted ? p.theme.tokens.content.disabled : p.theme.tokens.content.secondary};
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
`;

const BottomRow = styled('div')`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: ${p => p.theme.space.sm};
`;

const BottomLeft = styled('div')`
  display: flex;
  align-items: center;
  gap: ${p => p.theme.space.xs};
  min-width: 0;
  flex: 1;
`;

const BottomRight = styled('div')`
  display: flex;
  align-items: center;
  gap: ${p => p.theme.space.xs};
  flex-shrink: 0;
`;

const ProgressWrapper = styled('div')`
  display: flex;
  align-items: center;
  color: ${p => p.theme.tokens.content.secondary};
`;

const DetailRoot = styled('div')`
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
`;

const DetailHeader = styled('div')`
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: ${p => p.theme.space.md} ${p => p.theme.space.lg};
  border-bottom: 1px solid ${p => p.theme.tokens.border.primary};
  flex-shrink: 0;
`;

const DetailBody = styled('div')`
  flex: 1;
  overflow-y: auto;
  scrollbar-width: none;
  padding: ${p => p.theme.space.lg} 0;

  &::-webkit-scrollbar {
    display: none;
  }
`;
