import {useState} from 'react';
import styled from '@emotion/styled';

import * as Layout from 'sentry/components/layouts/thirds';
import {NoProjectMessage} from 'sentry/components/noProjectMessage';
import {PageFiltersContainer} from 'sentry/components/pageFilters/container';
import {t} from 'sentry/locale';
import {useOrganization} from 'sentry/utils/useOrganization';
import {IssueListContainer} from 'sentry/views/issueList';
import {IssueListFilters} from 'sentry/views/issueList/filters';
import {IssueInboxSection} from 'sentry/views/issueList/issueInboxSection';
import {IssueSortOptions} from 'sentry/views/issueList/utils';
import {useTopOffset} from 'sentry/views/navigation/useTopOffset';

const TITLE = t('Awaiting Input');
const DEFAULT_QUERY = 'is:unresolved';
const DEFAULT_SORT = IssueSortOptions.PROGRESS;

export default function IssueInboxPage() {
  const organization = useOrganization();
  const {contentTop} = useTopOffset();
  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [sort, setSort] = useState(DEFAULT_SORT);

  return (
    <IssueListContainer title={TITLE}>
      <Layout.Title>{TITLE}</Layout.Title>
      <PageFiltersContainer>
        <NoProjectMessage organization={organization}>
          <PageBody contentTop={contentTop}>
            <FiltersWrapper>
              <IssueListFilters
                query={query}
                sort={sort}
                onSearch={setQuery}
                onSortChange={newSort => setSort(newSort as IssueSortOptions)}
              />
            </FiltersWrapper>
            <InboxWrapper>
              <IssueInboxSection query={query} sort={sort} />
            </InboxWrapper>
          </PageBody>
        </NoProjectMessage>
      </PageFiltersContainer>
    </IssueListContainer>
  );
}

const PageBody = styled('div')<{contentTop: string}>`
  display: flex;
  flex-direction: column;
  height: calc(100dvh - ${p => p.contentTop});
  overflow: hidden;
  background-color: ${p => p.theme.tokens.background.primary};
`;

const FiltersWrapper = styled('div')`
  flex-shrink: 0;
  padding: ${p => p.theme.space.lg} ${p => p.theme.space.lg} 0;
`;

const InboxWrapper = styled('div')`
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: ${p => p.theme.space.lg};
  padding-top: 0;
  gap: 0;
`;
