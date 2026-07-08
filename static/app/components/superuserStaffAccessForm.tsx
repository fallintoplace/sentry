import {Fragment, useCallback, useEffect, useState} from 'react';
import {useMutation} from '@tanstack/react-query';

import {Alert} from '@sentry/scraps/alert';
import {Button} from '@sentry/scraps/button';
import {defaultFormOptions, useScrapsForm} from '@sentry/scraps/form';
import {Flex} from '@sentry/scraps/layout';

import {logout} from 'sentry/actionCreators/account';
import {LoadingIndicator} from 'sentry/components/loadingIndicator';
import {Override} from 'sentry/components/override';
import {WebAuthn} from 'sentry/components/webAuthn';
import {ErrorCodes} from 'sentry/constants/superuserAccessErrors';
import {t} from 'sentry/locale';
import {ConfigStore} from 'sentry/stores/configStore';
import type {Authenticator} from 'sentry/types/auth';
import {fetchMutation} from 'sentry/utils/queryClient';
import {useApi} from 'sentry/utils/useApi';

interface WebAuthnParams {
  challenge: string;
  response: string;
  isSuperuserModal?: boolean;
  superuserAccessCategory?: string;
  superuserReason?: string;
}

/**
 * Payload sent to the (staff|superuser) auth endpoint. The access-category and
 * reason fields are collected from the native `<form>` via FormData on submit —
 * they are rendered by the getsentry `superuser-access-category` override
 * (backed by the legacy form fields) which is intentionally left unmigrated.
 */
type AuthMutationData = {
  challenge?: string;
  isSuperuserModal?: boolean;
  response?: string;
  superuserAccessCategory?: string;
  superuserReason?: string;
};

interface Props {
  hasStaff: boolean;
}

function SuperuserStaffAccessForm({hasStaff}: Props) {
  const api = useApi();
  const authUrl = hasStaff ? '/staff-auth/' : '/auth/';

  const [authenticators, setAuthenticators] = useState<Authenticator[]>([]);
  const [error, setError] = useState(false);
  const [errorType, setErrorType] = useState('');
  const [showAccessForms, setShowAccessForms] = useState(true);
  const [superuserAccessCategory, setSuperuserAccessCategory] = useState('');
  const [superuserReason, setSuperuserReason] = useState('');
  const [isLoading, setIsLoading] = useState(true);

  const {mutateAsync: submitAuth} = useMutation({
    mutationFn: (data: AuthMutationData) =>
      fetchMutation({url: authUrl, method: 'PUT', data}),
  });

  const handleSuccess = useCallback(() => {
    window.location.reload();
  }, []);

  const handleError = useCallback((err: any) => {
    let newErrorType = '';
    if (err.status === 403) {
      if (err.responseJSON.detail.code === 'no_u2f') {
        newErrorType = ErrorCodes.NO_AUTHENTICATOR;
      } else {
        newErrorType = ErrorCodes.INVALID_PASSWORD;
      }
    } else if (err.status === 401) {
      newErrorType = ErrorCodes.INVALID_SSO_SESSION;
    } else if (err.status === 400) {
      if (err.responseJSON.detail.code === 'missing_password_or_u2f') {
        newErrorType = ErrorCodes.MISSING_PASSWORD_OR_U2F;
      } else {
        newErrorType = ErrorCodes.INVALID_ACCESS_CATEGORY;
      }
    } else if (err === ErrorCodes.NO_AUTHENTICATOR) {
      newErrorType = ErrorCodes.NO_AUTHENTICATOR;
    } else {
      newErrorType = ErrorCodes.UNKNOWN_ERROR;
    }
    setError(true);
    setErrorType(newErrorType);
    setShowAccessForms(true);
    setIsLoading(false);
  }, []);

  const handleLogout = useCallback(() => {
    const {superuserUrl} = window.__initialData.links;
    const urlOrigin =
      window.__initialData.customerDomain && superuserUrl
        ? superuserUrl
        : window.location.origin;

    const nextUrl = new URL('/auth/login/', urlOrigin);
    nextUrl.searchParams.set('next', window.location.href);

    logout(api, nextUrl.toString());
  }, [api]);

  // Fetch authenticators on mount (and, on local staff dev, immediately submit).
  useEffect(() => {
    const disableU2FForSUForm = ConfigStore.get('disableU2FForSUForm');

    // If using staff and on local dev, skip U2F and immediately submit
    if (hasStaff && disableU2FForSUForm) {
      submitAuth({superuserAccessCategory: '', superuserReason: ''})
        .then(handleSuccess)
        .catch(handleError);
      return;
    }

    (async () => {
      let nextAuthenticators: Authenticator[] = [];
      try {
        nextAuthenticators = (await api.requestPromise('/authenticators/')) ?? [];
      } catch {
        // ignore errors
      }
      setAuthenticators(nextAuthenticators);

      // Set the error state if there are no authenticators and U2F is on
      if (!nextAuthenticators.length && !disableU2FForSUForm) {
        handleError(ErrorCodes.NO_AUTHENTICATOR);
      }
      setIsLoading(false);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSubmitCOPS = () => {
    setSuperuserAccessCategory('cops_csm');
    setSuperuserReason('COPS and CSM use');
  };

  const handleSubmit = async (data: AuthMutationData) => {
    const disableU2FForSUForm = ConfigStore.get('disableU2FForSUForm');

    const suAccessCategory = superuserAccessCategory || data.superuserAccessCategory;
    const suReason = superuserReason || data.superuserReason;

    if (!authenticators.length && !disableU2FForSUForm) {
      handleError(ErrorCodes.NO_AUTHENTICATOR);
      return;
    }

    // Set state to setup for U2F tap
    if (showAccessForms && !disableU2FForSUForm) {
      setShowAccessForms(false);
      setSuperuserAccessCategory(suAccessCategory ?? '');
      setSuperuserReason(suReason ?? '');
      // If U2F is disabled, authenticate immediately
    } else {
      try {
        await submitAuth(data);
        handleSuccess();
      } catch (err) {
        handleError(err);
      }
    }
  };

  const handleWebAuthn = useCallback(
    async (data: WebAuthnParams) => {
      if (!hasStaff) {
        data.isSuperuserModal = true;
        data.superuserAccessCategory = superuserAccessCategory;
        data.superuserReason = superuserReason;
      }
      try {
        await submitAuth(data);
        handleSuccess();
      } catch (err) {
        handleError(err);
        // u2fInterface relies on this
        throw err;
      }
    },
    [
      hasStaff,
      superuserAccessCategory,
      superuserReason,
      submitAuth,
      handleSuccess,
      handleError,
    ]
  );

  const form = useScrapsForm({
    ...defaultFormOptions,
    defaultValues: {},
    onSubmit: async ({formApi}) => {
      // The access-category/reason fields are rendered by an unmigrated legacy
      // override, so read their values directly off the submitted native form.
      const formElement = document.getElementById(
        formApi.formId
      ) as HTMLFormElement | null;
      const formData = formElement ? new FormData(formElement) : new FormData();
      const suAccessCategory = formData.get('superuserAccessCategory');
      const suReason = formData.get('superuserReason');

      await handleSubmit({
        isSuperuserModal: true,
        superuserAccessCategory:
          typeof suAccessCategory === 'string' ? suAccessCategory : undefined,
        superuserReason: typeof suReason === 'string' ? suReason : undefined,
      });
    },
  });

  if (errorType === ErrorCodes.INVALID_SSO_SESSION) {
    handleLogout();
    return null;
  }

  if (hasStaff) {
    if (isLoading) {
      return <LoadingIndicator />;
    }

    return (
      <Fragment>
        {error && <Alert variant="danger">{errorType}</Alert>}
        <WebAuthn
          mode="sudo"
          authenticators={authenticators}
          onWebAuthn={handleWebAuthn}
        />
      </Fragment>
    );
  }

  return (
    <form.AppForm form={form}>
      {error && <Alert variant="danger">{errorType}</Alert>}
      {showAccessForms && <Override name="component:superuser-access-category" />}
      {!showAccessForms && (
        <WebAuthn
          mode="sudo"
          authenticators={authenticators}
          onWebAuthn={handleWebAuthn}
        />
      )}
      <Flex justify="between" align="center" gap="md">
        <Button type="submit" onClick={handleSubmitCOPS}>
          {t('COPS/CSM')}
        </Button>
        <form.SubmitButton>{t('Continue')}</form.SubmitButton>
      </Flex>
    </form.AppForm>
  );
}

export default SuperuserStaffAccessForm;
