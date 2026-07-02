import {Fragment} from 'react';
import {css} from '@emotion/react';
import styled from '@emotion/styled';

import {ActorAvatar} from '@sentry/scraps/avatar';
import {Button, LinkButton} from '@sentry/scraps/button';
import {DrawerBody, DrawerHeader} from '@sentry/scraps/drawer';
import {Container, Flex} from '@sentry/scraps/layout';
import {TabList, TabPanels, Tabs} from '@sentry/scraps/tabs';
import {Heading} from '@sentry/scraps/text';
import {Tooltip} from '@sentry/scraps/tooltip';

import {AssigneeSelectorDropdown} from 'sentry/components/assigneeSelectorDropdown';
import {IconCellSignal} from 'sentry/components/badge/iconCellSignal';
import {ErrorBoundary} from 'sentry/components/errorBoundary';
import {EventMessage} from 'sentry/components/events/eventMessage';
import {useHandleAssigneeChange} from 'sentry/components/group/assigneeSelector';
import {LoadingError} from 'sentry/components/loadingError';
import {LoadingIndicator} from 'sentry/components/loadingIndicator';
import {IconOpen, IconUser} from 'sentry/icons';
import {t} from 'sentry/locale';
import type {Group} from 'sentry/types/group';
import {PriorityLevel} from 'sentry/types/group';
import {getMessage, getTitle} from 'sentry/utils/events';
import {normalizeUrl} from 'sentry/utils/url/normalizeUrl';
import {useOrganization} from 'sentry/utils/useOrganization';
import {useProjects} from 'sentry/utils/useProjects';
import {useSyncedLocalStorageState} from 'sentry/utils/useSyncedLocalStorageState';
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
import {IssuePreviewAutofix} from 'sentry/views/issueDetails/issuePreview/issuePreviewAutofix';
import {IssuePreviewDetails} from 'sentry/views/issueDetails/issuePreview/issuePreviewDetails';
import {EventList} from 'sentry/views/issueDetails/eventList';
import {useGroup} from 'sentry/views/issueDetails/useGroup';
import {
  getGroupReprocessingStatus,
  ReprocessingStatus,
} from 'sentry/views/issueDetails/utils';

interface IssuePreviewDrawerProps {
  groupId: string;
}

export function IssuePreviewDrawer({groupId}: IssuePreviewDrawerProps) {
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
          <LinkButton
            to={issueDetailsUrl}
            size="xs"
            icon={<IconOpen />}
            style={{marginLeft: 'auto'}}
          >
            {t('Open Issue')}
          </LinkButton>
        </Flex>
      </DrawerHeader>
      <DrawerBody>
        {isPending && <LoadingIndicator />}
        {isError && <LoadingError />}
        {group && project && (
          <GroupDataContextProvider group={group} project={project}>
            <ErrorBoundary mini>
              <IssuePreviewContent />
            </ErrorBoundary>
          </GroupDataContextProvider>
        )}
      </DrawerBody>
    </Fragment>
  );
}

const INBOX_TAB_KEY = 'issue-inbox-selected-tab';

interface IssuePreviewContentProps {
  /**
   * When true, applies horizontal padding to title/action sections so the
   * tabs can span full width (the parent must have no horizontal padding).
   */
  fullWidthTabs?: boolean;
}

export function IssuePreviewContent({fullWidthTabs}: IssuePreviewContentProps) {
  const {group, project} = useGroupData();
  const {hasAutofix} = useAiConfig(group, project);
  const [activeTab, setActiveTab] = useSyncedLocalStorageState(INBOX_TAB_KEY, 'activity');

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
          <div>
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
          </div>
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
        <GroupActions
          group={group}
          project={project}
          disabled={disableActions}
          event={null}
        />
        <InboxPriorityButton group={group} />
        <InboxAssigneeButton group={group} />
      </ActionBarFlex>
      <Tabs value={activeTab} onChange={setActiveTab}>
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
            <Container paddingTop="md" paddingLeft="lg" paddingRight="lg">
              <ActivitySection
                group={group}
                variant="standalone"
                size="md"
                placeholder={t('Add a comment. Tag users with @, or teams with #')}
              />
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
      </Tabs>
    </Fragment>
  );
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
