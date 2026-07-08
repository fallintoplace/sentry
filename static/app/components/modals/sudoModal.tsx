import {Fragment, useCallback, useRef, useState} from 'react';
import styled from '@emotion/styled';
import {useMutation, useQuery} from '@tanstack/react-query';
import trimEnd from 'lodash/trimEnd';

import {Alert} from '@sentry/scraps/alert';
import {Button, LinkButton} from '@sentry/scraps/button';
import {defaultFormOptions, useScrapsForm} from '@sentry/scraps/form';
import {Flex} from '@sentry/scraps/layout';

import {logout} from 'sentry/actionCreators/account';
import type {ModalRenderProps} from 'sentry/actionCreators/modal';
import {
  getBoostrapTeamsQueryOptions,
  getBootstrapOrganizationQueryOptions,
  getBootstrapProjectsQueryOptions,
} from 'sentry/bootstrap/bootstrapRequests';
import {LoadingIndicator} from 'sentry/components/loadingIndicator';
import {Override} from 'sentry/components/override';
import {WebAuthn} from 'sentry/components/webAuthn';
import {ErrorCodes} from 'sentry/constants/superuserAccessErrors';
import {t} from 'sentry/locale';
import {ConfigStore} from 'sentry/stores/configStore';
import type {Authenticator} from 'sentry/types/auth';
import {getApiUrl} from 'sentry/utils/api/getApiUrl';
import {fetchMutation, useApiQuery} from 'sentry/utils/queryClient';
import {useApi} from 'sentry/utils/useApi';
import {useLocation} from 'sentry/utils/useLocation';
import {useNavigate} from 'sentry/utils/useNavigate';
import {useParams} from 'sentry/utils/useParams';
import {useUser} from 'sentry/utils/useUser';
import {TextBlock} from 'sentry/views/settings/components/text/textBlock';

interface WebAuthnParams {
  challenge: string;
  response: string;
  isSuperuserModal?: boolean;
  superuserAccessCategory?: string;
  superuserReason?: string;
}

type AuthPayload = {
  isSuperuserModal: boolean;
  challenge?: string;
  password?: string;
  response?: string;
  superuserAccessCategory?: string;
  superuserReason?: string;
};

type DefaultProps = {
  closeButton?: boolean;
};

type State = {
  error: boolean;
  errorType: string;
  showAccessForms: boolean;
  superuserAccessCategory: string;
  superuserReason: string;
};

type Props = DefaultProps &
  Pick<ModalRenderProps, 'Body' | 'Header'> & {
    closeModal: () => void;
    /**
     * User is a superuser without an active su session
     */
    isSuperuser?: boolean;
    needsReload?: boolean;
    /**
     * expects a function that returns a Promise
     */
    retryRequest?: () => Promise<any>;
  };

function SudoModal({
  closeModal,
  isSuperuser,
  needsReload,
  retryRequest,
  Header,
  Body,
  closeButton,
}: Props) {
  const user = useUser();
  const navigate = useNavigate();
  const params = useParams<{orgId?: string}>();
  const location = useLocation();
  const api = useApi();
  const [state, setState] = useState<State>({
    error: false,
    errorType: '',
    showAccessForms: true,
    superuserAccessCategory: '',
    superuserReason: '',
  });

  const {error, errorType, showAccessForms} = state;

  const orgSlug = params.orgId ?? null;
  // We have to wait for these requests to finish before we can sudo, otherwise
  // we'll overwrite the session cookie with a stale one.
  // Not sharing the bootstrap hooks to avoid mutating the store.
  const {isFetching: isOrganizationFetching} = useQuery(
    getBootstrapOrganizationQueryOptions(orgSlug)
  );
  const {isFetching: isTeamsFetching} = useQuery(getBoostrapTeamsQueryOptions(orgSlug));
  const {isFetching: isProjectsFetching} = useQuery(
    getBootstrapProjectsQueryOptions(orgSlug)
  );
  const bootstrapIsPending =
    isOrganizationFetching || isTeamsFetching || isProjectsFetching;

  // XXX(epurkhiser): Using isFetchedAfterMount here since the WebAuthn
  // authenticator will always produce a new challenge. We don't want to render
  // the WebAuthnAssert and then re-render with a different challenge, causing
  // the prompt to trigger twice.
  const {
    data: authenticators = [],
    isFetching: authenticatorsFetching,
    isFetchedAfterMount: authenticatorsLoaded,
  } = useApiQuery<Authenticator[]>([getApiUrl('/authenticators/')], {
    // Fetch authenticators after preload requests to avoid overwriting session cookie
    enabled: !bootstrapIsPending,
    staleTime: 0,
    retry: false,
    // Immeditealy refetch authenticators on window / tab focus. If a user had
    // multiple tabs open and required authentication in any other tabs we may
    // have stomped the session state the request sets, and will need to reload
    // session state immediately.
    refetchOnWindowFocus: true,
  });

  // Mirror mutable values into refs so the form's onSubmit callback always reads
  // the current values instead of a stale render closure.
  const stateRef = useRef(state);
  stateRef.current = state;
  const authenticatorsRef = useRef(authenticators);
  authenticatorsRef.current = authenticators;

  const {mutateAsync: authenticate} = useMutation({
    mutationFn: (data: AuthPayload) =>
      fetchMutation({method: 'PUT', url: getApiUrl('/auth/'), data}),
  });

  const handleSubmitCOPS = () => {
    setState(prevState => ({
      ...prevState,
      superuserAccessCategory: 'cops_csm',
      superuserReason: 'COPS and CSM use',
    }));
  };

  const handleChangeReason = (e: React.MouseEvent) => {
    // XXX(epurkhiser): We have to prevent default here to avoid react from
    // propagating this event up to the form and causing the form to be
    // submitted. This is happening because when the form is rendered the same
    // button is replaced with a button that has type="submit", this happens
    // before the event is propegated to the form, and by the time that handler
    // is run react thinks the button is type submit and will submit the form.
    //
    // See https://github.com/facebook/react/issues/8554#issuecomment-278580583
    e.preventDefault();

    setState(prevState => ({
      ...prevState,
      showAccessForms: true,
      superuserAccessCategory: '',
      superuserReason: '',
    }));
  };

  const handleSuccess = useCallback(() => {
    if (isSuperuser) {
      navigate(
        {pathname: location.pathname, state: {forceUpdate: new Date()}},
        {replace: true}
      );
      if (needsReload) {
        window.location.reload();
      }
      return;
    }

    if (!retryRequest) {
      closeModal();
      return;
    }

    retryRequest().then(() => {
      setState(prevState => ({...prevState, showAccessForms: true}));
      closeModal();
    });
  }, [closeModal, isSuperuser, location.pathname, navigate, needsReload, retryRequest]);

  const handleError = useCallback((err: any) => {
    let newErrorType = ''; // Create a new variable to store the error type

    if (err.status === 403) {
      if (err.responseJSON.detail.code === 'no_u2f') {
        newErrorType = ErrorCodes.NO_AUTHENTICATOR;
      } else {
        newErrorType = ErrorCodes.INVALID_PASSWORD;
      }
    } else if (err.status === 401) {
      newErrorType = ErrorCodes.INVALID_SSO_SESSION;
    } else if (err.status === 400) {
      newErrorType = ErrorCodes.INVALID_ACCESS_CATEGORY;
    } else if (err === ErrorCodes.NO_AUTHENTICATOR) {
      newErrorType = ErrorCodes.NO_AUTHENTICATOR;
    } else {
      newErrorType = ErrorCodes.UNKNOWN_ERROR;
    }

    setState(prevState => ({
      ...prevState,
      error: true,
      errorType: newErrorType,
      showAccessForms: true,
    }));
  }, []);

  const handleWebAuthn = useCallback(
    async (data: WebAuthnParams) => {
      // It's ok to throw from here, u2fInterface will handle it.
      await authenticate({
        ...data,
        isSuperuserModal: Boolean(isSuperuser),
        superuserAccessCategory: stateRef.current.superuserAccessCategory,
        superuserReason: stateRef.current.superuserReason,
      });
      handleSuccess();
    },
    [authenticate, handleSuccess, isSuperuser]
  );

  const form = useScrapsForm({
    ...defaultFormOptions,
    defaultValues: {password: ''},
    onSubmit: async ({value, formApi}) => {
      const disableU2FForSUForm = ConfigStore.get('disableU2FForSUForm');
      const isSelfHosted = ConfigStore.get('isSelfHosted');
      const validateSUForm = ConfigStore.get('validateSUForm');
      const currentAuthenticators = authenticatorsRef.current;

      // Whether the superuser access form (categories + reason) is currently
      // rendered — this mirrors the render-time branching in renderBodyContent.
      const isSuperuserAccessForm =
        Boolean(isSuperuser) &&
        ((!user.hasPasswordAuth && currentAuthenticators.length === 0) ||
          (!isSelfHosted && validateSUForm));

      if (isSuperuserAccessForm) {
        // The superuser access category / reason fields come from a getsentry
        // override that renders legacy (unbound) form fields, so read their
        // values directly from the DOM. COPS/CSM populates them via state first.
        const formEl = document.getElementById(formApi.formId);
        const formData = formEl instanceof HTMLFormElement ? new FormData(formEl) : null;
        const domCategory = formData?.get('superuserAccessCategory');
        const domReason = formData?.get('superuserReason');

        const suAccessCategory =
          stateRef.current.superuserAccessCategory ||
          (typeof domCategory === 'string' ? domCategory : '');
        const suReason =
          stateRef.current.superuserReason ||
          (typeof domReason === 'string' ? domReason : '');

        if (!currentAuthenticators.length && !disableU2FForSUForm) {
          handleError(ErrorCodes.NO_AUTHENTICATOR);
          return;
        }

        if (stateRef.current.showAccessForms && !disableU2FForSUForm) {
          setState(prevState => ({
            ...prevState,
            showAccessForms: false,
            superuserAccessCategory: suAccessCategory,
            superuserReason: suReason,
          }));
          return;
        }

        try {
          await authenticate({
            isSuperuserModal: true,
            superuserAccessCategory: suAccessCategory,
            superuserReason: suReason,
          });
          handleSuccess();
        } catch (err) {
          formApi.reset();
          handleError(err);
        }
        return;
      }

      // Default (re-authenticate / password) flow.
      try {
        await authenticate({
          isSuperuserModal: Boolean(isSuperuser),
          password: value.password,
        });
        handleSuccess();
      } catch (err) {
        formApi.reset();
        handleError(err);
      }
    },
  });

  const getAuthLoginPath = (): string => {
    const authLoginPath = `/auth/login/?next=${encodeURIComponent(window.location.href)}`;
    const {superuserUrl} = window.__initialData.links;
    if (window.__initialData?.customerDomain && superuserUrl) {
      return `${trimEnd(superuserUrl, '/')}${authLoginPath}`;
    }
    return authLoginPath;
  };

  const renderBodyContent = () => {
    const isSelfHosted = ConfigStore.get('isSelfHosted');
    const validateSUForm = ConfigStore.get('validateSUForm');

    if (errorType === ErrorCodes.INVALID_SSO_SESSION) {
      logout(api, getAuthLoginPath());
      return null;
    }

    if (authenticatorsFetching || !authenticatorsLoaded || bootstrapIsPending) {
      return <LoadingIndicator />;
    }

    if (
      (!user.hasPasswordAuth && authenticators.length === 0) ||
      (isSuperuser && !isSelfHosted && validateSUForm)
    ) {
      return (
        <Fragment>
          <StyledTextBlock>
            {isSuperuser
              ? t(
                  'You are attempting to access a resource that requires superuser access, please re-authenticate as a superuser.'
                )
              : t('You will need to reauthenticate to continue')}
          </StyledTextBlock>
          {error && <Alert variant="danger">{errorType}</Alert>}
          {isSuperuser ? (
            <form.AppForm form={form}>
              {!isSelfHosted && showAccessForms && (
                <Override name="component:superuser-access-category" />
              )}
              {!isSelfHosted && !showAccessForms && (
                <WebAuthn
                  mode="sudo"
                  authenticators={authenticators}
                  onWebAuthn={handleWebAuthn}
                />
              )}
              <FormFooter justify="between" align="center" gap="md">
                <Flex align="center" margin="0 3xl">
                  {showAccessForms ? (
                    <Button type="submit" onClick={handleSubmitCOPS}>
                      {t('COPS/CSM')}
                    </Button>
                  ) : (
                    <Button variant="transparent" size="sm" onClick={handleChangeReason}>
                      {t('Change reason')}
                    </Button>
                  )}
                </Flex>
                <form.SubmitButton>
                  {showAccessForms ? t('Continue') : t('Re-authenticate')}
                </form.SubmitButton>
              </FormFooter>
            </form.AppForm>
          ) : (
            <LinkButton variant="primary" href={getAuthLoginPath()}>
              {t('Continue')}
            </LinkButton>
          )}
        </Fragment>
      );
    }

    return (
      <Fragment>
        <StyledTextBlock>
          {isSuperuser
            ? t(
                'You are attempting to access a resource that requires superuser access, please re-authenticate as a superuser.'
              )
            : t('Help us keep your account safe by confirming your identity.')}
        </StyledTextBlock>

        {error && <Alert variant="danger">{errorType}</Alert>}

        <form.AppForm form={form}>
          {user.hasPasswordAuth && (
            <form.AppField name="password">
              {field => (
                <field.Layout.Stack label={t('Password')}>
                  <field.Input
                    type="password"
                    autoFocus
                    value={field.state.value}
                    onChange={field.handleChange}
                  />
                </field.Layout.Stack>
              )}
            </form.AppField>
          )}

          <WebAuthn
            mode="sudo"
            authenticators={authenticators}
            onWebAuthn={handleWebAuthn}
          />

          {!(!user.hasPasswordAuth && authenticators.length === 0) && (
            <FormFooter justify="end">
              <form.SubmitButton>{t('Confirm Password')}</form.SubmitButton>
            </FormFooter>
          )}
        </form.AppForm>
      </Fragment>
    );
  };

  return (
    <Fragment>
      <Header closeButton={closeButton}>
        <h4>{t('Confirm Password to Continue')}</h4>
      </Header>
      <Body>{renderBodyContent()}</Body>
    </Fragment>
  );
}

export default SudoModal;

const StyledTextBlock = styled(TextBlock)`
  margin-bottom: ${p => p.theme.space.md};
`;

const FormFooter = styled(Flex)`
  margin-top: ${p => p.theme.space.xl};
`;
