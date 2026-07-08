import {useState} from 'react';
import styled from '@emotion/styled';

import {Button} from '@sentry/scraps/button';
import {Stack} from '@sentry/scraps/layout';
import {Text} from '@sentry/scraps/text';

import {t} from 'sentry/locale';
import type {ChallengeData} from 'sentry/types/auth';

import {handleEnroll} from './handlers';

interface WebAuthnEnrollProps {
  challengeData: ChallengeData;
  /**
   * Called with the enrollment result once the browser has produced a
   * WebAuthn attestation. The `challenge` and `response` values are what the
   * enrollment endpoint expects.
   */
  onEnroll: (result: {challenge: string; response: string}) => void;
}

const UNSUPPORTED_NOTICE = t(
  'Your browser does not support WebAuthn (passkey). You need to use a different two-factor method or switch to a browser that supports it (Google Chrome or Microsoft Edge)'
);

const FAILURE_MESSAGE = t('There was a problem enrolling, please try again.');

export function WebAuthnEnroll({challengeData, onEnroll}: WebAuthnEnrollProps) {
  const isSupported = !!window.PublicKeyCredential;
  const challenge = JSON.stringify(challengeData);

  const [activated, setActivated] = useState(false);
  const [hasResponse, setHasResponse] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const triggerEnroll = async () => {
    setActivated(false);
    setError(null);

    try {
      const webAuthnResponse = await handleEnroll(challengeData);

      if (!webAuthnResponse) {
        setError(FAILURE_MESSAGE);
        return;
      }

      setActivated(true);
      setHasResponse(true);
      onEnroll({challenge, response: webAuthnResponse});
    } catch (err) {
      setError(FAILURE_MESSAGE);
      setActivated(false);
    }
  };

  return (
    <Stack gap="sm" align="start">
      <EnrollButton onClick={triggerEnroll} disabled={!isSupported || activated}>
        {hasResponse ? t('Enrolled!') : t('Start Enrollment')}
      </EnrollButton>
      {!isSupported && (
        <Text variant="danger" size="sm">
          {UNSUPPORTED_NOTICE}
        </Text>
      )}
      {error && (
        <Text variant="danger" size="sm">
          {error}
        </Text>
      )}
    </Stack>
  );
}

const EnrollButton = styled(Button)`
  align-self: self-end;
`;
