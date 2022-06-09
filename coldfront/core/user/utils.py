import abc
from enum import Enum
import logging
import re
from typing import Iterable, List

from django.contrib.auth.models import User
from django.db.models import Q
from django.utils.module_loading import import_string

from coldfront.core.utils.common import import_from_settings

logger = logging.getLogger(__name__)

class UserSearch(abc.ABC):

    def __init__(self, user_search_string, search_by):
        self.user_search_string = user_search_string
        self.search_by = search_by

    @abc.abstractmethod
    def search_a_user(self, user_search_string=None, search_by='all_fields'):
        pass

    def search(self):
        if len(self.user_search_string.split()) > 1:
            search_by = 'username_only'
            matches = []
            number_of_usernames_found = 0
            users_not_found = []

            user_search_string = sorted(list(set(self.user_search_string.split())))
            for username in user_search_string:
                match = self.search_a_user(username, search_by)
                if match:
                    matches.extend(match)
        else:
            matches = self.search_a_user(self.user_search_string, self.search_by)

        return matches


class LocalUserSearch(UserSearch):
    search_source = 'local'

    def search_a_user(self, user_search_string=None, search_by='all_fields'):
        size_limit = 50
        if user_search_string and search_by == 'all_fields':
            entries = User.objects.filter(
                Q(username__icontains=user_search_string) |
                Q(first_name__icontains=user_search_string) |
                Q(last_name__icontains=user_search_string) |
                Q(email__icontains=user_search_string)
            ).filter(Q(is_active=True)).distinct()[:size_limit]

        elif user_search_string and search_by == 'username_only':
            entries = User.objects.filter(username=user_search_string, is_active=True)
        else:
            entries = User.objects.all()[:size_limit]

        users = []
        for idx, user in enumerate(entries, 1):
            if user:
                user_dict = {
                    'last_name': user.last_name,
                    'first_name': user.first_name,
                    'username': user.username,
                    'email': user.email,
                    'source': self.search_source,
                }
                users.append(user_dict)

        logger.info("Local user search for %s found %s results", user_search_string, len(users))
        return users


class CombinedUserSearch:

    def __init__(self, user_search_string, search_by, usernames_names_to_exclude=[]):
        self.USER_SEARCH_CLASSES = import_from_settings('ADDITIONAL_USER_SEARCH_CLASSES', [])
        self.USER_SEARCH_CLASSES.insert(0, 'coldfront.core.user.utils.LocalUserSearch')
        self.user_search_string = user_search_string
        self.search_by = search_by
        self.usernames_names_to_exclude = usernames_names_to_exclude

    def search(self):

        matches = []
        usernames_not_found = []
        usernames_found = []


        for search_class in self.USER_SEARCH_CLASSES:
            cls = import_string(search_class)
            search_class_obj = cls(self.user_search_string, self.search_by)
            users = search_class_obj.search()

            for user in users:
                username = user.get('username')
                if username not in usernames_found and username not in self.usernames_names_to_exclude:
                    usernames_found.append(username)
                    matches.append(user)

        if len(self.user_search_string.split()) > 1:
            number_of_usernames_searched = len(self.user_search_string.split())
            number_of_usernames_found = len(usernames_found)
            usernames_not_found = list(set(self.user_search_string.split()) - set(usernames_found) - set(self.usernames_names_to_exclude))
        else:
            number_of_usernames_searched = None
            number_of_usernames_found = None
            usernames_not_found = None

        context = {
            'matches': matches,
            'number_of_usernames_searched': number_of_usernames_searched,
            'number_of_usernames_found': number_of_usernames_found,
            'usernames_not_found': usernames_not_found
        }
        return context


class InferUsernameFromEmail:
    """
    Some imported usernames do not match the naming convention for usernames
    in Coldfront. As such, we can infer the 'correct' username from the user's
    email address.
    """
    class KeyOption(Enum):
        ID = 0,
        ORIGINAL_USERNAME = 1,
        EMAIL = 2,

    def __init__(self, users: Iterable[User], key_option: KeyOption, unique_names: bool) -> None:
        """
        Creates the utility.

        :param users: List of users
        :param key_option: What to use for the keys of the dictionary. See search.
        :param unique_names: If true, ensure that all generated usernames are unique.
        """
        self.users = users
        self.key_option = key_option
        self.unique_names = unique_names
    
    def next_username(self, username: str, used_usernames: set[str]):
        """
        Provides the next username in numerical order.
        """
        while username in used_usernames:
            match = re.search("\d+$", username)

            if match is None:
                username = username + '0'
            else:
                next = int(match[0]) + 1
                username = re.sub("\d+$", str(next), username)

        return username

    def search(self) -> List[dict]:
        """
        Performs the search. Returns dictionary with KeyOption as keys
        and the inferred usernames as values.
        """
        context = []

        if self.unique_names:
            used_usernames = set()

        for user in self.users:
            username = user.email.split('@')[0]
            
            if self.unique_names and username in used_usernames:
                username = self.next_username(username, used_usernames)
            
            used_usernames.add(username)
            
            match self.key_option:
                case self.KeyOption.ID:
                    data = { user.pk: username }
                case self.KeyOption.EMAIL:
                    data = { user.email: username }
                case self.KeyOption.ORIGINAL_USERNAME:
                    data = { user.username: username }
            context.append(data)
        
        return context
