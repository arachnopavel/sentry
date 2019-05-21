import React from 'react';

import {mount} from 'enzyme';
import OnboardingPlatform from 'app/views/onboarding/platform';
import {createProject} from 'app/actionCreators/projects';

jest.mock('app/actionCreators/projects');

describe('OnboardingWelcome', function() {
  it('calls onUpdate when setting the platform', function() {
    const onUpdate = jest.fn();

    const wrapper = mount(
      <OnboardingPlatform active onUpdate={onUpdate} />,
      TestStubs.routerContext()
    );

    wrapper
      .find('[data-test-id="platform-csharp"]')
      .first()
      .simulate('click');

    expect(onUpdate).toHaveBeenCalled();
  });

  it('calls onReturnToStep when clearing the platform', function() {
    const onUpdate = jest.fn();
    const onReturnToStep = jest.fn();

    const wrapper = mount(
      <OnboardingPlatform
        platform="csharp"
        onUpdate={onUpdate}
        onReturnToStep={onReturnToStep}
      />,
      TestStubs.routerContext()
    );

    wrapper
      .find('ClearButton')
      .first()
      .simulate('click');

    expect(onReturnToStep).toHaveBeenCalled();
  });

  it('creates a project when no project exists', async function() {
    const onComplete = jest.fn();

    const wrapper = mount(
      <OnboardingPlatform active onComplete={onComplete} platform="csharp" />,
      TestStubs.routerContext()
    );

    const button = wrapper.find('Button[priority="primary"]');

    expect(button.text()).toEqual('Create Project');

    let resolveProjectCreate;
    createProject.mockReturnValue(
      new Promise((resolve, reject) => (resolveProjectCreate = resolve))
    );

    wrapper
      .find('Button[priority="primary"]')
      .first()
      .simulate('click');

    expect(wrapper.find('Button[priority="primary"]').text()).toEqual(
      'Creating Project...'
    );

    resolveProjectCreate({id: 1, slug: 'test-project'});
    await tick();
    await tick();
    await tick();

    expect(wrapper.find('Button[priority="primary"]').text()).toEqual('Project Created');

    expect(onComplete).toHaveBeenCalled();
  });
});
