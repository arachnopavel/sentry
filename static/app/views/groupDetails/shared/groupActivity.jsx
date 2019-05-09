import PropTypes from 'prop-types';
import React from 'react';
import _ from 'lodash';

import {
  addErrorMessage,
  addLoadingMessage,
  clearIndicators,
} from 'app/actionCreators/indicator';
import {createNote, deleteNote, updateNote} from 'app/actionCreators/group';
import {t} from 'app/locale';
import {uniqueId} from 'app/utils/guid';
import ActivityAuthor from 'app/components/activity/author';
import ActivityItem from 'app/components/activity/item';
import Avatar from 'app/components/avatar';
import ConfigStore from 'app/stores/configStore';
import ErrorBoundary from 'app/components/errorBoundary';
import GroupActivityItem from 'app/views/groupDetails/shared/groupActivityItem';
import MemberListStore from 'app/stores/memberListStore';
import Note from 'app/components/activity/note';
import NoteInputWithStorage from 'app/components/activity/note/inputWithStorage';
import ProjectsStore from 'app/stores/projectsStore';
import SentryTypes from 'app/sentryTypes';
import withApi from 'app/utils/withApi';
import withOrganization from 'app/utils/withOrganization';

function makeDefaultErrorJson() {
  return {detail: t('Unknown error. Please try again.')};
}

class GroupActivity extends React.Component {
  // TODO(dcramer): only re-render on group/activity change
  static propTypes = {
    api: PropTypes.object,
    organization: SentryTypes.Organization.isRequired,
    group: SentryTypes.Group,
  };

  state = {
    createBusy: false,
    preview: false,
    error: false,
    inputId: uniqueId(),
  };

  getMemberList = (memberList, sessionUser) =>
    _.uniqBy(memberList, ({id}) => id).filter(({id}) => sessionUser.id !== id);

  handleNoteDelete = async item => {
    const {api, group} = this.props;

    addLoadingMessage(t('Removing comment...'));

    try {
      await deleteNote(api, group, item);
      clearIndicators();
    } catch (_err) {
      addErrorMessage(t('Failed to delete comment'));
    }
  };

  handleNoteCreate = async note => {
    const {api, group} = this.props;

    this.setState({
      createBusy: true,
    });

    addLoadingMessage(t('Posting comment...'));

    try {
      await createNote(api, group, note);

      this.setState({
        createBusy: false,
        preview: false,
        mentions: [],

        // This is used as a `key` to Note Input so that after successful post
        // we reset the value of the input
        inputId: uniqueId(),
      });
      clearIndicators();
    } catch (error) {
      this.setState({
        createBusy: false,
        preview: false,
        error: true,
        errorJSON: error.responseJSON || makeDefaultErrorJson(),
      });
      addErrorMessage(t('Unable to post comment'));
    }
  };

  handleNoteUpdate = async (note, item) => {
    const {api, group} = this.props;

    this.setState({
      updateBusy: true,
    });
    addLoadingMessage(t('Updating comment...'));

    try {
      await updateNote(api, group, item, note);
      this.setState({
        updateBusy: false,
        preview: false,
      });
      clearIndicators();
    } catch (error) {
      this.setState({
        updateBusy: false,
        preview: false,
        error: true,
        errorJSON: error.responseJSON || makeDefaultErrorJson(),
      });
      addErrorMessage(t('Unable to update comment'));
    }
  };

  getMentionableTeams = projectSlug => {
    return (
      ProjectsStore.getBySlug(projectSlug) || {
        teams: [],
      }
    ).teams;
  };

  render() {
    const {organization, group} = this.props;
    const me = ConfigStore.get('user');
    const memberList = this.getMemberList(MemberListStore.getAll(), me);
    const teams = this.getMentionableTeams(group && group.project && group.project.slug);
    const noteProps = {
      group,
      memberList,
      teams,
      placeholder: t(
        'Add details or updates to this event. \nTag users with @, or teams with #'
      ),
    };

    return (
      <div className="row">
        <div className="col-md-9">
          <div>
            <ActivityItem author={{type: 'user', user: me}}>
              {() => (
                <NoteInputWithStorage
                  key={this.state.inputId}
                  storageKey="groupinput:latest"
                  itemKey={group.id}
                  onCreate={this.handleNoteCreate}
                  busy={this.state.createBusy}
                  error={this.state.error}
                  errorJSON={this.state.errorJSON}
                  {...noteProps}
                />
              )}
            </ActivityItem>

            {group.activity.map(item => {
              const authorName = item.user ? item.user.name : 'Sentry';

              if (item.type === 'note') {
                return (
                  <ErrorBoundary mini key={`note-${item.id}`}>
                    <Note
                      item={item}
                      id={`note-${item.id}`}
                      author={{
                        name: authorName,
                        avatar: <Avatar user={item.user} size={38} />,
                      }}
                      onDelete={this.handleNoteDelete}
                      onUpdate={this.handleNoteUpdate}
                      busy={this.state.updateBusy}
                      {...noteProps}
                    />
                  </ErrorBoundary>
                );
              } else {
                return (
                  <ErrorBoundary mini key={`item-${item.id}`}>
                    <ActivityItem
                      item={item}
                      author={{type: item.user ? 'user' : 'system', user: item.user}}
                      date={item.dateCreated}
                      header={
                        <GroupActivityItem
                          organization={organization}
                          author={<ActivityAuthor>{authorName}</ActivityAuthor>}
                          item={item}
                          orgId={this.props.params.orgId}
                          projectId={group.project.slug}
                        />
                      }
                    />
                  </ErrorBoundary>
                );
              }
            })}
          </div>
        </div>
      </div>
    );
  }
}

export {GroupActivity};
export default withApi(withOrganization(GroupActivity));