import {Fragment, useCallback, useMemo, useState} from 'react';
import {css} from '@emotion/react';
import styled from '@emotion/styled';

import {ActorAvatar} from '@sentry/scraps/avatar';
import {Button, LinkButton} from '@sentry/scraps/button';
import {DrawerBody, DrawerHeader} from '@sentry/scraps/drawer';
import {Container, Flex} from '@sentry/scraps/layout';
import {TabList, TabPanels, Tabs} from '@sentry/scraps/tabs';
import {Heading, Text} from '@sentry/scraps/text';
import {Tooltip} from '@sentry/scraps/tooltip';

import {AssigneeSelectorDropdown} from 'sentry/components/assigneeSelectorDropdown';
import {IconCellSignal} from 'sentry/components/badge/iconCellSignal';
import {ErrorBoundary} from 'sentry/components/errorBoundary';
import {EventMessage} from 'sentry/components/events/eventMessage';
import {
  getAutofixArtifactFromSection,
  getOrderedAutofixSections,
  isCodeChangesSection,
  isPullRequestsArtifact,
  isPullRequestsSection,
  isRootCauseSection,
  isSolutionSection,
  useExplorerAutofix,
} from 'sentry/components/events/autofix/useExplorerAutofix';
import {useHandleAssigneeChange} from 'sentry/components/group/assigneeSelector';
import {useLinkedPullRequests} from 'sentry/components/group/externalIssuesList/linkedPullRequests';
import {LoadingError} from 'sentry/components/loadingError';
import {LoadingIndicator} from 'sentry/components/loadingIndicator';
import {IconBug, IconChevron, IconOpen, IconUser} from 'sentry/icons';
import {t} from 'sentry/locale';
import {defined} from 'sentry/utils/defined';
import type {Group} from 'sentry/types/group';
import {GroupStatus, PriorityLevel} from 'sentry/types/group';
import type {LinkedPullRequest} from 'sentry/types/integrations';
import {getMessage, getTitle} from 'sentry/utils/events';
import {normalizeUrl} from 'sentry/utils/url/normalizeUrl';
import {useOrganization} from 'sentry/utils/useOrganization';
import {useProjects} from 'sentry/utils/useProjects';
import {GroupActions} from 'sentry/views/issueDetails/actions/index';
import {ActivitySection} from 'sentry/views/issueDetails/activitySection';
import {IssueDetailsContextProvider} from 'sentry/views/issueDetails/context';
import {
  GroupDataContextProvider,
  useGroupData,
} from 'sentry/views/issueDetails/groupDataContext';
import {GroupPriority} from 'sentry/views/issueDetails/groupPriority';
import {GroupStatusSubtitle} from 'sentry/views/issueDetails/header/groupStatusSubtitle';
import {IssueIdBreadcrumb} from 'sentry/views/issueDetails/header/issueIdBreadcrumb';
import {useAiConfig} from 'sentry/views/issueDetails/hooks/useAiConfig';
import {useIssueProgress} from 'sentry/views/issueList/useIssueProgress';
import {
  formatProgressState,
  getProgressIcon,
} from 'sentry/views/issueList/utils/progress';
import {IssuePreviewAutofix} from 'sentry/views/issueDetails/issuePreview/issuePreviewAutofix';
import {IssuePreviewDetails} from 'sentry/views/issueDetails/issuePreview/issuePreviewDetails';
import {EventList} from 'sentry/views/issueDetails/eventList';
import {useGroup} from 'sentry/views/issueDetails/useGroup';
import {useGroupEvent} from 'sentry/views/issueDetails/useGroupEvent';
import {
  getGroupReprocessingStatus,
  ReprocessingStatus,
} from 'sentry/views/issueDetails/utils';
import {ExternalIssueSidebarList} from 'sentry/views/issueDetails/sidebar/externalIssueSidebarList';

interface IssuePreviewDrawerProps {
  groupId: string;
  activeTab?: string;
  onNavigateNext?: () => void;
  onNavigatePrev?: () => void;
  onTabChange?: (tab: string) => void;
}

export function IssuePreviewDrawer({
  groupId,
  onNavigatePrev,
  onNavigateNext,
  activeTab,
  onTabChange,
}: IssuePreviewDrawerProps) {
  const organization = useOrganization();
  const {data: group, isPending, isError} = useGroup({groupId});
  const {projects} = useProjects();
  const project = projects.find(p => p.id === group?.project.id) ?? group?.project;

  const issueDetailsUrl = normalizeUrl(
    `/organizations/${organization.slug}/issues/${groupId}/`
  );

  return (
    <Fragment>
      <DrawerHeader>
        <Flex justify="between" align="center" flex="1">
          {group && project && <IssueIdBreadcrumb group={group} project={project} />}
          <Flex align="center" gap="xs" style={{marginLeft: 'auto'}}>
            {(onNavigatePrev || onNavigateNext) && (
              <Flex align="center">
                <Button
                  size="xs"
                  icon={<IconChevron direction="up" />}
                  aria-label={t('Previous issue')}
                  disabled={!onNavigatePrev}
                  onClick={onNavigatePrev}
                />
                <Button
                  size="xs"
                  icon={<IconChevron direction="down" />}
                  aria-label={t('Next issue')}
                  disabled={!onNavigateNext}
                  onClick={onNavigateNext}
                />
              </Flex>
            )}
            <LinkButton to={issueDetailsUrl} size="xs" icon={<IconOpen />}>
              {t('Open Issue')}
            </LinkButton>
          </Flex>
        </Flex>
      </DrawerHeader>
      <DrawerBody>
        {isPending && <LoadingIndicator />}
        {isError && <LoadingError />}
        {group && project && (
          <GroupDataContextProvider group={group} project={project}>
            <ErrorBoundary mini>
              <IssuePreviewContent activeTab={activeTab} onTabChange={onTabChange} />
            </ErrorBoundary>
          </GroupDataContextProvider>
        )}
      </DrawerBody>
    </Fragment>
  );
}


interface IssuePreviewContentProps {
  /**
   * When true, applies horizontal padding to title/action sections so the
   * tabs can span full width (the parent must have no horizontal padding).
   */
  fullWidthTabs?: boolean;
  /** Controlled tab key — lifted to the drawer so tab is preserved across issue navigation. */
  activeTab?: string;
  onTabChange?: (tab: string) => void;
}

export function IssuePreviewContent({
  fullWidthTabs,
  activeTab: activeTabProp,
  onTabChange,
}: IssuePreviewContentProps) {
  const {group, project} = useGroupData();
  const {hasAutofix} = useAiConfig(group, project);
  const [activeTab, setActiveTabState] = useState(activeTabProp ?? 'activity');

  const setActiveTab = useCallback(
    (tab: string) => {
      setActiveTabState(tab);
      onTabChange?.(tab);
    },
    [onTabChange]
  );

  const onActivateAutofixTab = useCallback(() => setActiveTab('autofix'), [setActiveTab]);

  const {data: linkedPRsData} = useLinkedPullRequests({group});
  const openLinkedPR = linkedPRsData?.pullRequests.find(pr => pr.status === 'open');
  const mergedLinkedPR = linkedPRsData?.pullRequests.find(pr => pr.status === 'merged');
  // If there's a merged PR and the issue isn't yet resolved, Resolve becomes primary
  const hasMergedPRPendingResolve =
    !!mergedLinkedPR && group.status !== GroupStatus.RESOLVED;

  const {data: progressData} = useIssueProgress([group.id]);
  const progressState = progressData?.results[group.id]?.progress ?? null;

  const {title: primaryTitle} = getTitle(group);
  const secondaryTitle = getMessage(group);
  const disableActions = [
    ReprocessingStatus.REPROCESSING,
    ReprocessingStatus.REPROCESSED_AND_HASNT_EVENT,
  ].includes(getGroupReprocessingStatus(group));

  return (
    <Fragment>
      <Container
        paddingBottom="lg"
        paddingLeft={fullWidthTabs ? 'lg' : undefined}
        paddingRight={fullWidthTabs ? 'lg' : undefined}
        borderBottom="muted"
      >
        <Flex direction="column" gap="xs">
          <Flex justify="between" align="start" gap="sm">
            <Flex direction="column" flex="1" style={{minWidth: 0}}>
              <Tooltip
                title={primaryTitle}
                skipWrapper
                isHoverable
                showOnlyOnOverflow
                delay={1000}
              >
                <Heading as="h3" size="lg" ellipsis>
                  {primaryTitle}
                </Heading>
              </Tooltip>
              <EventMessage
                level={group.level}
                message={secondaryTitle}
                type={group.type}
              />
            </Flex>
            {progressState && (
              <Flex align="center" gap="xs" style={{flexShrink: 0}}>
                {getProgressIcon(progressState)}
                <Text size="sm">{formatProgressState(progressState)}</Text>
              </Flex>
            )}
          </Flex>
          <GroupStatusSubtitle group={group} project={project} />
        </Flex>
      </Container>
      <ActionBarFlex
        paddingTop="lg"
        paddingBottom="lg"
        borderBottom="muted"
        align="center"
        wrap="wrap"
        gap="sm"
        fullWidthTabs={fullWidthTabs}
      >
        {hasAutofix && !hasMergedPRPendingResolve && (
          <InboxAutofixCta
            group={group}
            onActivateAutofixTab={onActivateAutofixTab}
            linkedOpenPR={openLinkedPR}
          />
        )}
        <GroupActions
          group={group}
          project={project}
          disabled={disableActions}
          event={null}
          actionsAreSecondary={hasAutofix && !hasMergedPRPendingResolve}
        />
        <InboxPriorityButton group={group} />
        <InboxAssigneeButton group={group} />
      </ActionBarFlex>
      <InboxTabs value={activeTab} onChange={setActiveTab}>
        <Container paddingTop="md" paddingBottom="md" paddingLeft="lg" paddingRight="lg" borderBottom="muted">
          <TabList variant="floating">
            <TabList.Item key="activity">{t('Activity')}</TabList.Item>
            {hasAutofix ? (
              <TabList.Item key="autofix">{t('Autofix')}</TabList.Item>
            ) : null}
            <TabList.Item key="details">{t('Details')}</TabList.Item>
            <TabList.Item key="events">{t('Events')}</TabList.Item>
          </TabList>
        </Container>
        <TabPanels>
          <TabPanels.Item key="activity">
            {/* Own container so SectionDivider (hr) that FoldSection always appends
                becomes the last child → auto-hidden by its &:last-child rule */}
            <ExternalLinksContainer paddingTop="md" paddingLeft="lg" paddingRight="lg">
              <InboxExternalLinks group={group} />
            </ExternalLinksContainer>
            <Container paddingTop="2xl" paddingLeft="lg" paddingRight="lg">
              <Container paddingBottom="md">
                <Heading as="h3" size="md">{t('Activity Feed')}</Heading>
              </Container>
              <Container paddingLeft="xl">
                <ActivitySection
                  group={group}
                  variant="standalone"
                  size="md"
                  placeholder={t('Add a comment. Tag users with @, or teams with #')}
                />
              </Container>
            </Container>
          </TabPanels.Item>
          {hasAutofix ? (
            <TabPanels.Item key="autofix">
              <Container paddingTop="md" paddingLeft="lg" paddingRight="lg">
                <IssuePreviewAutofix group={group} project={project} />
              </Container>
            </TabPanels.Item>
          ) : null}
          <TabPanels.Item key="details">
            <Container paddingTop="md" paddingLeft="lg" paddingRight="lg">
              <IssueDetailsContextProvider>
                <IssuePreviewDetails group={group} project={project} />
              </IssueDetailsContextProvider>
            </Container>
          </TabPanels.Item>
          <TabPanels.Item key="events">
            <Container paddingTop="md" paddingLeft="lg" paddingRight="lg">
              <EventList group={group} />
            </Container>
          </TabPanels.Item>
        </TabPanels>
      </InboxTabs>
    </Fragment>
  );
}

interface InboxAutofixCtaProps {
  group: Group;
  onActivateAutofixTab: () => void;
  linkedOpenPR?: LinkedPullRequest;
}

function InboxAutofixCta({
  group,
  onActivateAutofixTab,
  linkedOpenPR,
}: InboxAutofixCtaProps) {
  const autofix = useExplorerAutofix(group.id);

  const sections = useMemo(
    () => getOrderedAutofixSections(autofix.runState),
    [autofix.runState]
  );

  const runId = autofix.runState?.run_id;
  const lastSection = sections[sections.length - 1];

  // A linked open PR (from GitHub integration) always wins — show View PR first
  if (linkedOpenPR) {
    return (
      <LinkButton
        size="sm"
        variant="primary"
        href={linkedOpenPR.externalUrl}
        external
        openInNewTab
        icon={<IconOpen />}
      >
        {t('View PR')}
      </LinkButton>
    );
  }

  if (autofix.isLoading) {
    return null;
  }

  // Fallback: autofix run state may have created a PR not yet reflected in linked PRs
  const completedPR = Object.values(autofix.runState?.repo_pr_states ?? {}).find(
    pr => pr.pr_creation_status === 'completed' && pr.pr_url
  );
  if (completedPR) {
    return (
      <LinkButton
        size="sm"
        variant="primary"
        href={completedPR.pr_url!}
        external
        openInNewTab
        icon={<IconOpen />}
      >
        {t('View PR')}
      </LinkButton>
    );
  }

  if (!sections.length) {
    return (
      <Button
        size="sm"
        variant="primary"
        icon={<IconBug />}
        onClick={() => {
          onActivateAutofixTab();
          autofix.startStep('root_cause');
        }}
      >
        {t('Start Analysis')}
      </Button>
    );
  }

  if (autofix.isPolling) {
    return (
      <Button
        size="sm"
        variant="primary"
        icon={<LoadingIndicator size={14} mini />}
        onClick={onActivateAutofixTab}
      >
        {t('Analyzing…')}
      </Button>
    );
  }

  if (lastSection?.status !== 'completed') {
    return null;
  }

  const artifact = getAutofixArtifactFromSection(lastSection);

  if (isRootCauseSection(lastSection) && artifact) {
    return (
      <Button
        size="sm"
        variant="primary"
        onClick={() => {
          onActivateAutofixTab();
          autofix.startStep('solution', {runId});
        }}
      >
        {t('Make a Plan')}
      </Button>
    );
  }

  if (isSolutionSection(lastSection) && artifact) {
    return (
      <Button
        size="sm"
        variant="primary"
        onClick={() => {
          onActivateAutofixTab();
          autofix.startStep('code_changes', {runId});
        }}
      >
        {t('Write a Code Fix')}
      </Button>
    );
  }

  if (isCodeChangesSection(lastSection) && artifact && defined(runId)) {
    return (
      <Button
        size="sm"
        variant="primary"
        onClick={() => {
          onActivateAutofixTab();
          autofix.createPR(runId);
        }}
      >
        {t('Draft a PR')}
      </Button>
    );
  }

  if (isPullRequestsSection(lastSection)) {
    const prArtifact = getAutofixArtifactFromSection(lastSection);
    if (isPullRequestsArtifact(prArtifact)) {
      const completedPR = prArtifact.find(
        pr => pr.pr_creation_status === 'completed' && pr.pr_url
      );
      if (completedPR) {
        return (
          <LinkButton
            size="sm"
            variant="primary"
            href={completedPR.pr_url!}
            external
            openInNewTab
            icon={<IconOpen />}
          >
            {t('View PR')}
          </LinkButton>
        );
      }
    }
    return (
      <Button size="sm" variant="primary" disabled>
        {t('View PR')}
      </Button>
    );
  }

  return null;
}

// Force tab panels to fill full width. TabPanelWrap in horizontal orientation
// sets height:100% but omits width, which can leave panels narrower than the container.
const InboxTabs = styled(Tabs)`
  & [role='tabpanel'] {
    width: 100%;
  }
` as typeof Tabs;

const ExternalLinksContainer = styled(Container)`
  overflow: hidden;
`;

function InboxExternalLinks({group}: {group: Group}) {
  const {data: event} = useGroupEvent({groupId: group.id, eventId: 'latest'});
  if (!event) {
    return null;
  }
  return <ExternalIssueSidebarList group={group} event={event} />;
}

const ActionBarFlex = styled(Flex)<{fullWidthTabs?: boolean}>`
  ${p =>
    p.fullWidthTabs &&
    css`
      padding-left: ${p.theme.space.lg};
      padding-right: ${p.theme.space.lg};
    `}
`;

function InboxPriorityButton({group}: {group: Group}) {
  const bars =
    group.priority === PriorityLevel.HIGH
      ? 3
      : group.priority === PriorityLevel.LOW
        ? 1
        : 2;

  return (
    <GroupPriority
      group={group}
      trigger={(triggerProps, _isOpen) => (
        <PriorityButton
          {...(triggerProps as any)}
          aria-label={t('Modify issue priority')}
          icon={<IconCellSignal bars={bars} />}
          size="sm"
        />
      )}
    />
  );
}

function InboxAssigneeButton({group}: {group: Group}) {
  const organization = useOrganization();
  const {handleAssigneeChange, assigneeLoading} = useHandleAssigneeChange({
    organization,
    group,
  });

  return (
    <AssigneeSelectorDropdown
      group={group}
      loading={assigneeLoading}
      onAssign={actor => handleAssigneeChange(actor)}
      onClear={() => handleAssigneeChange(null)}
      trigger={(triggerProps, _isOpen) => (
        <AvatarTrigger
          {...(triggerProps as any)}
          role="button"
          aria-label={t('Modify issue assignee')}
        >
          {group.assignedTo ? (
            <ActorAvatar actor={group.assignedTo} size={24} hasTooltip />
          ) : (
            <IconUser size="md" />
          )}
        </AvatarTrigger>
      )}
    />
  );
}

// Matches the overflow "..." button appearance exactly — secondary outline, sm size, icon only.
const PriorityButton = styled(Button)``;

// Bare clickable span — no button chrome, just the avatar, height matches sm buttons (32px).
const AvatarTrigger = styled('span')`
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 32px;
  cursor: pointer;
  border-radius: 50%;

  &:focus-visible {
    outline: 2px solid ${p => p.theme.tokens.focus.default};
    outline-offset: 2px;
  }
`;
