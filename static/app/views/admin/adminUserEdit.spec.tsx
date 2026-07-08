import {UserFixture} from 'sentry-fixture/user';

import {
  render,
  renderGlobalModal,
  screen,
  userEvent,
  waitFor,
} from 'sentry-test/reactTestingLibrary';

import AdminUserEdit from 'sentry/views/admin/adminUserEdit';

describe('AdminUserEdit', () => {
  const ENDPOINT = '/users/1/';

  const routerConfig = {
    location: {pathname: '/manage/users/1/'},
    route: '/manage/users/:id/',
  };

  it('renders the user details and gates saving on changes', async () => {
    MockApiClient.addMockResponse({
      url: ENDPOINT,
      method: 'GET',
      body: UserFixture({name: 'Foo Bar'}),
    });
    const putMock = MockApiClient.addMockResponse({
      url: ENDPOINT,
      method: 'PUT',
      body: UserFixture({name: 'New Name'}),
    });

    render(<AdminUserEdit />, {initialRouterConfig: routerConfig});

    const nameInput = await screen.findByRole('textbox', {name: 'Name'});
    expect(nameInput).toHaveValue('Foo Bar');

    // Save is disabled until the form is dirty (preserves requireChanges).
    expect(screen.getByRole('button', {name: 'Save Changes'})).toBeDisabled();

    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'New Name');

    const saveButton = screen.getByRole('button', {name: 'Save Changes'});
    expect(saveButton).toBeEnabled();
    await userEvent.click(saveButton);

    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith(
        ENDPOINT,
        expect.objectContaining({
          method: 'PUT',
          data: expect.objectContaining({name: 'New Name'}),
        })
      )
    );
  });

  it('hard deletes the user from the remove modal', async () => {
    MockApiClient.addMockResponse({
      url: ENDPOINT,
      method: 'GET',
      body: UserFixture(),
    });
    const deleteMock = MockApiClient.addMockResponse({
      url: ENDPOINT,
      method: 'DELETE',
      body: {},
    });

    render(<AdminUserEdit />, {initialRouterConfig: routerConfig});
    renderGlobalModal();

    await userEvent.click(await screen.findByRole('button', {name: 'Remove User'}));

    await userEvent.click(
      screen.getByRole('radio', {name: 'Permanently remove the user and their data.'})
    );
    await userEvent.click(screen.getByRole('button', {name: 'Permanently Delete User'}));

    await waitFor(() =>
      expect(deleteMock).toHaveBeenCalledWith(
        ENDPOINT,
        expect.objectContaining({
          method: 'DELETE',
          data: {hardDelete: true, organizations: []},
        })
      )
    );
  });
});
