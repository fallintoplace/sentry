import {Fragment, useState} from 'react';
import {useTheme} from '@emotion/react';
import styled from '@emotion/styled';
import {useMutation, useQueryClient} from '@tanstack/react-query';
import {z} from 'zod';

import {Button} from '@sentry/scraps/button';
import {defaultFormOptions, useScrapsForm} from '@sentry/scraps/form';
import {Flex} from '@sentry/scraps/layout';
import {useModal} from '@sentry/scraps/modal';

import {addErrorMessage, addSuccessMessage} from 'sentry/actionCreators/indicator';
import type {ModalRenderProps} from 'sentry/actionCreators/modal';
import {RadioGroup} from 'sentry/components/forms/controls/radioGroup';
import {LoadingError} from 'sentry/components/loadingError';
import {LoadingIndicator} from 'sentry/components/loadingIndicator';
import {t} from 'sentry/locale';
import type {User} from 'sentry/types/user';
import {getApiUrl} from 'sentry/utils/api/getApiUrl';
import {fetchMutation, setApiQueryData, useApiQuery} from 'sentry/utils/queryClient';
import {RequestError} from 'sentry/utils/requestError/requestError';
import {useNavigate} from 'sentry/utils/useNavigate';
import {useParams} from 'sentry/utils/useParams';

const schema = z.object({
  name: z.string().min(1, t('Name is required')),
  username: z.string().min(1, t('Username is required')),
  email: z.string().min(1, t('Email is required')),
  isActive: z.boolean(),
  isStaff: z.boolean(),
  isSuperuser: z.boolean(),
});

// The `/users/$userId/` PUT endpoint accepts these editable fields.
type UserUpdatePayload = {
  email: string;
  isActive: boolean;
  isStaff: boolean;
  isSuperuser: boolean;
  name: string;
  username: string;
};

function toFormValues(user: User): UserUpdatePayload {
  return {
    name: user.name,
    username: user.username,
    email: user.email,
    isActive: user.isActive,
    isStaff: user.isStaff,
    isSuperuser: user.isSuperuser,
  };
}

const REMOVE_BUTTON_LABEL = {
  disable: t('Disable User'),
  delete: t('Permanently Delete User'),
};

type DeleteType = 'disable' | 'delete';

type RemoveModalProps = ModalRenderProps & {
  onRemove: (type: DeleteType) => void;
  user: User;
};

function RemoveUserModal({user, onRemove, closeModal}: RemoveModalProps) {
  const [deleteType, setDeleteType] = useState<DeleteType>('disable');

  const handleRemove = () => {
    onRemove(deleteType);
    closeModal();
  };

  return (
    <Fragment>
      <RadioGroup
        value={deleteType}
        label={t('Remove user %s', user.email)}
        onChange={type => setDeleteType(type)}
        choices={[
          ['disable', t('Disable the account.')],
          ['delete', t('Permanently remove the user and their data.')],
        ]}
      />
      <ModalFooter>
        <Button variant="danger" onClick={handleRemove}>
          {REMOVE_BUTTON_LABEL[deleteType]}
        </Button>
        <Button onClick={closeModal}>{t('Cancel')}</Button>
      </ModalFooter>
    </Fragment>
  );
}

function AdminUserEditForm({
  user,
  userEndpoint,
}: {
  user: User;
  userEndpoint: ReturnType<typeof getApiUrl>;
}) {
  const {openModal} = useModal();
  const theme = useTheme();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const updateMutation = useMutation({
    mutationFn: (data: UserUpdatePayload) =>
      fetchMutation<User>({url: userEndpoint, method: 'PUT', data}),
    onSuccess: response => {
      setApiQueryData(queryClient, [userEndpoint], response);
      addSuccessMessage(t('User account updated.'));
    },
    onError: error => {
      const detail =
        error instanceof RequestError ? error.responseJSON?.detail : undefined;
      addErrorMessage(
        typeof detail === 'string' ? detail : t('Failed to update user account.')
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () =>
      fetchMutation({
        url: userEndpoint,
        method: 'DELETE',
        data: {hardDelete: true, organizations: []},
      }),
    onSuccess: () => {
      addSuccessMessage(t("%s's account has been deleted.", user.email));
      navigate('/manage/users/', {replace: true});
    },
  });

  const form = useScrapsForm({
    ...defaultFormOptions,
    defaultValues: toFormValues(user),
    validators: {onDynamic: schema},
    onSubmit: ({value}) =>
      updateMutation
        .mutateAsync(value)
        .then(() => form.reset(value))
        .catch(() => {}),
  });

  const deactivateMutation = useMutation({
    mutationFn: () =>
      fetchMutation<User>({
        url: userEndpoint,
        method: 'PUT',
        data: {isActive: false},
      }),
    onSuccess: response => {
      setApiQueryData(queryClient, [userEndpoint], response);
      form.reset(toFormValues(response));
      addSuccessMessage(t("%s's account has been deactivated.", response.email));
    },
  });

  const removeUser = (actionType: DeleteType) =>
    actionType === 'delete' ? deleteMutation.mutate() : deactivateMutation.mutate();

  const openDeleteModal = () =>
    openModal(opts => <RemoveUserModal user={user} onRemove={removeUser} {...opts} />);

  return (
    <form.AppForm form={form}>
      <form.FieldGroup title={t('User details')}>
        <form.AppField name="name">
          {field => (
            <field.Layout.Row label={t('Name')} required>
              <field.Input value={field.state.value} onChange={field.handleChange} />
            </field.Layout.Row>
          )}
        </form.AppField>

        <form.AppField name="username">
          {field => (
            <field.Layout.Row
              label={t('Username')}
              hintText={t('The username is the unique id of the user in the system')}
              required
            >
              <field.Input value={field.state.value} onChange={field.handleChange} />
            </field.Layout.Row>
          )}
        </form.AppField>

        <form.AppField name="email">
          {field => (
            <field.Layout.Row
              label={t('Email')}
              hintText={t('The users primary email address')}
              required
            >
              <field.Input value={field.state.value} onChange={field.handleChange} />
            </field.Layout.Row>
          )}
        </form.AppField>

        <form.AppField name="isActive">
          {field => (
            <field.Layout.Row
              label={t('Active')}
              hintText={t(
                'Designates whether this user should be treated as active. Unselect this instead of deleting accounts.'
              )}
            >
              <field.Switch checked={field.state.value} onChange={field.handleChange} />
            </field.Layout.Row>
          )}
        </form.AppField>

        <form.AppField name="isStaff">
          {field => (
            <field.Layout.Row
              label={t('Admin')}
              hintText={t(
                'Designates whether this user can perform administrative functions.'
              )}
            >
              <field.Switch checked={field.state.value} onChange={field.handleChange} />
            </field.Layout.Row>
          )}
        </form.AppField>

        <form.AppField name="isSuperuser">
          {field => (
            <field.Layout.Row
              label={t('Superuser')}
              hintText={t(
                'Designates whether this user has all permissions without explicitly assigning them.'
              )}
            >
              <field.Switch checked={field.state.value} onChange={field.handleChange} />
            </field.Layout.Row>
          )}
        </form.AppField>
      </form.FieldGroup>

      <Flex justify="end" gap="md" padding="md">
        <Button
          onClick={openDeleteModal}
          style={{marginRight: theme.space.md}}
          variant="danger"
        >
          {t('Remove User')}
        </Button>
        <form.Subscribe selector={state => state.isDirty}>
          {isDirty => (
            <form.SubmitButton disabled={!isDirty}>{t('Save Changes')}</form.SubmitButton>
          )}
        </form.Subscribe>
      </Flex>
    </form.AppForm>
  );
}

function AdminUserEdit() {
  const {id} = useParams<{id: string}>();
  const userEndpoint = getApiUrl('/users/$userId/', {path: {userId: id}});

  const {
    data: user,
    isPending,
    isError,
    refetch,
  } = useApiQuery<User>([userEndpoint], {
    staleTime: 0,
  });

  if (isPending) {
    return <LoadingIndicator />;
  }

  if (isError) {
    return <LoadingError onRetry={refetch} />;
  }

  if (!user) {
    return null;
  }

  return (
    <Fragment>
      <h3>{t('Users')}</h3>
      <p>{t('Editing user: %s', user.email)}</p>
      <AdminUserEditForm user={user} userEndpoint={userEndpoint} />
    </Fragment>
  );
}

const ModalFooter = styled('div')`
  display: grid;
  grid-auto-flow: column;
  gap: ${p => p.theme.space.md};
  justify-content: end;
  padding: 20px 30px;
  margin: 20px -30px -30px;
  border-top: 1px solid ${p => p.theme.tokens.border.primary};
`;

export default AdminUserEdit;
