import datetime
import itertools
from multiprocessing.managers import BaseManager
import json
import re
from typing import Any, Iterable, List, Optional
from django import http

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.decorators import user_passes_test, login_required
from django.contrib.auth.models import User, Group
from django.db import IntegrityError
from coldfront.core.user.models import UserProfile
from coldfront.core.utils.common import import_from_settings
from django.contrib.messages.views import SuccessMessageMixin
from django.core import serializers
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q, QuerySet
from django.forms import formset_factory
from django.http import (HttpResponse, HttpResponseRedirect, JsonResponse)
from django.forms.models import model_to_dict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView
from django.views.generic.base import TemplateView
from django.views.generic.edit import FormView

from coldfront.core.allocation.models import (Allocation, AllocationAdminNote, AllocationAttribute, AllocationAttributeChangeRequest, AllocationChangeRequest,
                                              AllocationStatusChoice,
                                              AllocationUser, AllocationUserNote,
                                              AllocationUserStatusChoice)
from coldfront.core.allocation.signals import (allocation_activate_user,
                                               allocation_remove_user)
from coldfront.core.grant.models import Grant
from coldfront.core.project.forms import (ProjectAddUserForm,
                                          ProjectAddUsersToAllocationForm,
                                          ProjectRemoveUserForm, ProjectRenameForm,
                                          ProjectReviewEmailForm,
                                          ProjectReviewForm, ProjectSearchForm, ProjectSelectForm,
                                          ProjectUserUpdateForm, ProjectImportForm)
from coldfront.core.project.models import (Project, ProjectAdminComment, ProjectReview,
                                           ProjectReviewStatusChoice,
                                           ProjectStatusChoice, ProjectUser, ProjectUserMessage,
                                           ProjectUserRoleChoice,
                                           ProjectUserStatusChoice)
from coldfront.core.publication.models import Publication
from coldfront.core.research_output.models import ResearchOutput
from coldfront.core.resource.models import Resource
from coldfront.core.user.forms import UserSearchForm
from coldfront.core.user.utils import CombinedUserSearch
from coldfront.core.utils.common import get_domain_url, import_from_settings
from coldfront.core.utils.mail import send_email, send_email_template

EMAIL_ENABLED = import_from_settings('EMAIL_ENABLED', False)
ALLOCATION_ENABLE_ALLOCATION_RENEWAL = import_from_settings(
    'ALLOCATION_ENABLE_ALLOCATION_RENEWAL', True)
ALLOCATION_DEFAULT_ALLOCATION_LENGTH = import_from_settings(
    'ALLOCATION_DEFAULT_ALLOCATION_LENGTH', 365)

if EMAIL_ENABLED:
    EMAIL_DIRECTOR_EMAIL_ADDRESS = import_from_settings(
        'EMAIL_DIRECTOR_EMAIL_ADDRESS')
    EMAIL_SENDER = import_from_settings('EMAIL_SENDER')

class ProjectDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = Project
    template_name = 'project/project_detail.html'
    context_object_name = 'project'

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        if self.request.user.has_perm('project.can_view_all_projects'):
            return True

        project_obj = self.get_object()

        if project_obj.projectuser_set.filter(user=self.request.user, status__name='Active').exists():
            return True

        messages.error(
            self.request, 'You do not have permission to view the previous page.')
        return False

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Can the user update the project?
        if self.request.user.is_superuser:
            context['is_allowed_to_update_project'] = True
        elif self.object.projectuser_set.filter(user=self.request.user).exists():
            project_user = self.object.projectuser_set.get(
                user=self.request.user)
            if project_user.role.name == 'Manager':
                context['is_allowed_to_update_project'] = True
            else:
                context['is_allowed_to_update_project'] = False
        else:
            context['is_allowed_to_update_project'] = False

        # Only show 'Active Users'
        project_users = self.object.projectuser_set.filter(
            status__name='Active').order_by('user__username')

        context['mailto'] = 'mailto:' + \
            ','.join([user.user.email for user in project_users])

        if self.request.user.is_superuser or self.request.user.has_perm('allocation.can_view_all_allocations'):
            allocations = Allocation.objects.prefetch_related(
                'resources').filter(project=self.object).order_by('-end_date')
        else:
            if self.object.status.name in ['Active', 'New', ]:
                allocations = Allocation.objects.filter(
                    Q(project=self.object) &
                    Q(project__projectuser__user=self.request.user) &
                    Q(project__projectuser__status__name__in=['Active', ]) &
                    Q(allocationuser__user=self.request.user) &
                    Q(allocationuser__status__name__in=['Active', ])
                ).distinct().order_by('-end_date')
            else:
                allocations = Allocation.objects.prefetch_related(
                    'resources').filter(project=self.object)

        context['publications'] = Publication.objects.filter(
            project=self.object, status='Active').order_by('-year')
        context['research_outputs'] = ResearchOutput.objects.filter(
            project=self.object).order_by('-created')
        context['grants'] = Grant.objects.filter(
            project=self.object, status__name__in=['Active', 'Pending', 'Archived'])
        context['allocations'] = allocations
        context['project_users'] = project_users
        context['ALLOCATION_ENABLE_ALLOCATION_RENEWAL'] = ALLOCATION_ENABLE_ALLOCATION_RENEWAL

        try:
            context['ondemand_url'] = settings.ONDEMAND_URL
        except AttributeError:
            pass

        return context


class ProjectListView(LoginRequiredMixin, ListView):

    model = Project
    template_name = 'project/project_list.html'
    prefetch_related = ['pi', 'status', 'field_of_science', ]
    context_object_name = 'project_list'
    paginate_by = 25

    def get_queryset(self):

        order_by = self.request.GET.get('order_by')
        if order_by:
            direction = self.request.GET.get('direction')
            if direction == 'asc':
                direction = ''
            else:
                direction = '-'
            order_by = direction + order_by
        else:
            order_by = 'id'

        project_search_form = ProjectSearchForm(self.request.GET)

        if project_search_form.is_valid():
            data = project_search_form.cleaned_data
            if data.get('show_all_projects') and (self.request.user.is_superuser or self.request.user.has_perm('project.can_view_all_projects')):
                projects = Project.objects.prefetch_related('pi', 'field_of_science', 'status',).filter(
                    status__name__in=['New', 'Active', ]).order_by(order_by)
            else:
                projects = Project.objects.prefetch_related('pi', 'field_of_science', 'status',).filter(
                    Q(status__name__in=['New', 'Active', ]) &
                    Q(projectuser__user=self.request.user) &
                    Q(projectuser__status__name='Active')
                ).order_by(order_by)

            # Last Name
            if data.get('last_name'):
                projects = projects.filter(
                    pi__last_name__icontains=data.get('last_name'))

            # Username
            if data.get('username'):
                projects = projects.filter(
                    Q(pi__username__icontains=data.get('username')) |
                    Q(projectuser__user__username__icontains=data.get('username')) &
                    Q(projectuser__status__name='Active')
                )

            # Field of Science
            if data.get('field_of_science'):
                projects = projects.filter(
                    field_of_science__description__icontains=data.get('field_of_science'))

        else:
            projects = Project.objects.prefetch_related('pi', 'field_of_science', 'status',).filter(
                Q(status__name__in=['New', 'Active', ]) &
                Q(projectuser__user=self.request.user) &
                Q(projectuser__status__name='Active')
            ).order_by(order_by)

        return projects.distinct()

    def get_context_data(self, **kwargs):

        context = super().get_context_data(**kwargs)
        projects_count = self.get_queryset().count()
        context['projects_count'] = projects_count

        project_search_form = ProjectSearchForm(self.request.GET)
        if project_search_form.is_valid():
            context['project_search_form'] = project_search_form
            data = project_search_form.cleaned_data
            filter_parameters = ''
            for key, value in data.items():
                if value:
                    if isinstance(value, list):
                        for ele in value:
                            filter_parameters += '{}={}&'.format(key, ele)
                    else:
                        filter_parameters += '{}={}&'.format(key, value)
            context['project_search_form'] = project_search_form
        else:
            filter_parameters = None
            context['project_search_form'] = ProjectSearchForm()

        order_by = self.request.GET.get('order_by')
        if order_by:
            direction = self.request.GET.get('direction')
            filter_parameters_with_order_by = filter_parameters + \
                'order_by=%s&direction=%s&' % (order_by, direction)
        else:
            filter_parameters_with_order_by = filter_parameters

        if filter_parameters:
            context['expand_accordion'] = 'show'

        context['filter_parameters'] = filter_parameters
        context['filter_parameters_with_order_by'] = filter_parameters_with_order_by

        project_list = context.get('project_list')
        # project_list = Project.objects.all()
        paginator = Paginator(project_list, self.paginate_by)

        page = self.request.GET.get('page')

        try:
            project_list = paginator.page(page)
        except PageNotAnInteger:
            project_list = paginator.page(1)
        except EmptyPage:
            project_list = paginator.page(paginator.num_pages)

        return context


class ProjectArchivedListView(LoginRequiredMixin, ListView):

    model = Project
    template_name = 'project/project_archived_list.html'
    prefetch_related = ['pi', 'status', 'field_of_science', ]
    context_object_name = 'project_list'
    paginate_by = 10

    def get_queryset(self):

        order_by = self.request.GET.get('order_by')
        if order_by:
            direction = self.request.GET.get('direction')
            if direction == 'asc':
                direction = ''
            else:
                direction = '-'
            order_by = direction + order_by
        else:
            order_by = 'id'

        project_search_form = ProjectSearchForm(self.request.GET)

        if project_search_form.is_valid():
            data = project_search_form.cleaned_data
            if data.get('show_all_projects') and (self.request.user.is_superuser or self.request.user.has_perm('project.can_view_all_projects')):
                projects = Project.objects.prefetch_related('pi', 'field_of_science', 'status',).filter(
                    status__name__in=['Archived', ]).order_by(order_by)
            else:

                projects = Project.objects.prefetch_related('pi', 'field_of_science', 'status',).filter(
                    Q(status__name__in=['Archived', ]) &
                    Q(projectuser__user=self.request.user) &
                    Q(projectuser__status__name='Active')
                ).order_by(order_by)

            # Last Name
            if data.get('last_name'):
                projects = projects.filter(
                    pi__last_name__icontains=data.get('last_name'))

            # Username
            if data.get('username'):
                projects = projects.filter(
                    pi__username__icontains=data.get('username'))

            # Field of Science
            if data.get('field_of_science'):
                projects = projects.filter(
                    field_of_science__description__icontains=data.get('field_of_science'))

        else:
            projects = Project.objects.prefetch_related('pi', 'field_of_science', 'status',).filter(
                Q(status__name__in=['Archived', ]) &
                Q(projectuser__user=self.request.user) &
                Q(projectuser__status__name='Active')
            ).order_by(order_by)

        return projects

    def get_context_data(self, **kwargs):

        context = super().get_context_data(**kwargs)
        projects_count = self.get_queryset().count()
        context['projects_count'] = projects_count
        context['expand'] = False

        project_search_form = ProjectSearchForm(self.request.GET)
        if project_search_form.is_valid():
            context['project_search_form'] = project_search_form
            data = project_search_form.cleaned_data
            filter_parameters = ''
            for key, value in data.items():
                if value:
                    if isinstance(value, list):
                        for ele in value:
                            filter_parameters += '{}={}&'.format(key, ele)
                    else:
                        filter_parameters += '{}={}&'.format(key, value)
            context['project_search_form'] = project_search_form
        else:
            filter_parameters = None
            context['project_search_form'] = ProjectSearchForm()

        order_by = self.request.GET.get('order_by')
        if order_by:
            direction = self.request.GET.get('direction')
            filter_parameters_with_order_by = filter_parameters + \
                'order_by=%s&direction=%s&' % (order_by, direction)
        else:
            filter_parameters_with_order_by = filter_parameters

        if filter_parameters:
            context['expand_accordion'] = 'show'

        context['filter_parameters'] = filter_parameters
        context['filter_parameters_with_order_by'] = filter_parameters_with_order_by

        project_list = context.get('project_list')
        paginator = Paginator(project_list, self.paginate_by)

        page = self.request.GET.get('page')

        try:
            project_list = paginator.page(page)
        except PageNotAnInteger:
            project_list = paginator.page(1)
        except EmptyPage:
            project_list = paginator.page(paginator.num_pages)

        return context


def archive_project(project: Project):
    '''
    Archives the specified project.

    :param project: The project to archive.
    '''
    project_status_archive = ProjectStatusChoice.objects.get(
        name='Archived')
    allocation_status_expired = AllocationStatusChoice.objects.get(
        name='Expired')
    end_date = datetime.datetime.now()
    project.status = project_status_archive
    project.save()
    for allocation in project.allocation_set.filter(status__name='Active'):
        allocation.status = allocation_status_expired
        allocation.end_date = end_date
        allocation.save()


class ProjectArchiveProjectView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'project/project_archive.html'

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))

        if project_obj.pi == self.request.user:
            return True

        if project_obj.projectuser_set.filter(user=self.request.user, role__name='Manager', status__name='Active').exists():
            return True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pk = self.kwargs.get('pk')
        project = get_object_or_404(Project, pk=pk)

        context['project'] = project

        return context

    def post(self, request, *args, **kwargs):
        pk = self.kwargs.get('pk')
        project = get_object_or_404(Project, pk=pk)
        archive_project(project)
        return redirect(reverse('project-detail', kwargs={'pk': project.pk}))


class ProjectCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = Project
    template_name_suffix = '_create_form'
    fields = ['title', 'description', 'field_of_science', ]

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        if self.request.user.userprofile.is_pi:
            return True

    def form_valid(self, form):
        project_obj = form.save(commit=False)
        form.instance.pi = self.request.user
        form.instance.status = ProjectStatusChoice.objects.get(name='New')
        project_obj.save()
        self.object = project_obj

        project_user_obj = ProjectUser.objects.create(
            user=self.request.user,
            project=project_obj,
            role=ProjectUserRoleChoice.objects.get(name='Manager'),
            status=ProjectUserStatusChoice.objects.get(name='Active')
        )

        return super().form_valid(form)

    def get_success_url(self):
        return reverse('project-detail', kwargs={'pk': self.object.pk})


class ProjectUpdateView(SuccessMessageMixin, LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Project
    template_name_suffix = '_update_form'
    fields = ['title', 'description', 'field_of_science', ]
    success_message = 'Project updated.'

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        project_obj = self.get_object()

        if project_obj.pi == self.request.user:
            return True

        if project_obj.projectuser_set.filter(user=self.request.user, role__name='Manager', status__name='Active').exists():
            return True

    def dispatch(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))
        if project_obj.status.name not in ['Active', 'New', ]:
            messages.error(request, 'You cannot update an archived project.')
            return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': project_obj.pk}))
        else:
            return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return reverse('project-detail', kwargs={'pk': self.object.pk})


def assign_user(user: User, project_obj: Project):
    '''
    Assigns the specified user to the project_obj

    :param user: The user to assign
    :param project_obj: The project object to add the user into
    '''
    ProjectUser.objects.create(
        user=user,
        project=project_obj,
        role=ProjectUserRoleChoice.objects.get(name='Manager'),
        status=ProjectUserStatusChoice.objects.get(name='Active')
    )


class ProjectMergeView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    model = Project
    template_name = 'project/project_merge.html'

    def test_func(self) -> Optional[bool]:
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        if self.request.user.userprofile.is_pi:
            return True

    def get_proj_list(self):

        project_list = [
            {
                'id': project.id,
                'pi': project.pi,
                'title': project.title,
                'description': project.description
            }

            for project in Project.objects.exclude(status=ProjectStatusChoice.objects.get(name='Archived'))
            
        ]

        return project_list

    def post(self, request, *args, **kwargs):   
        def _combine_objects(from_ids: list, to_id: int, objects: BaseManager):
            '''
            Moves Django models from one project to another. Make sure the model has project_id.

            :param from_ids: A list of ids representing the project to take the models from.
            :param to_id: An id representing the project to give the models to.
            :param objects: The BaseManager for the particular model, to be used to filter from_ids.
            '''
            members = objects.filter(project_id__in = from_ids)
            for member in members:
                member.project_id = to_id
                # member.pk = None      # Uncomment if you want to keep the old projects.
                try:
                    member.save()
                except IntegrityError:
                    # Member already exists.
                    member.delete()
                    pass

        proj_list = self.get_proj_list()

        formset = formset_factory(ProjectSelectForm, max_num=len(proj_list))
        formset = formset(request.POST, initial=proj_list, prefix='projectform')

        projs_to_merge = []
        proj_ids = []

        if formset.is_valid:
            for form in formset:
                proj_form_data = form
                if proj_form_data['selected'].data:
                    proj: Project = Project.objects.get(
                        id = proj_form_data.initial['id']
                    )
                    proj_ids.append(proj_form_data.initial['id'])

                    projs_to_merge.append(proj)
        
        if projs_to_merge:
            merged_proj: Project = Project.objects.create(
                pi = self.request.user,
                status = projs_to_merge[0].status,
                title = request.POST['title'],
                description = request.POST['description'],
            )

            # Grants
            _combine_objects(proj_ids, merged_proj.id, Grant.objects)
            
            # Publications
            _combine_objects(proj_ids, merged_proj.id, Publication.objects)

            # User
            _combine_objects(proj_ids, merged_proj.id, ProjectUser.objects)

            # Allocations
            _combine_objects(proj_ids, merged_proj.id, Allocation.objects)

            # Resources
            # Resources are tied to allocations. Since the IDs of allocations are not
            # being changed in merge, resources should remain attached to each allocation.
            # THIS WILL NOT BE THE CASE IF WE DECIDE TO KEEP THE OLD ALLOCATIONS, as the
            # new allocations will change IDs.
            # Also, the code here doesn't work.
            # _combine_objects(proj_ids, merged_proj.id, Resource.objects)

            # Admin comments, user messages, reviews, and resource outputs
            _combine_objects(proj_ids, merged_proj.id, ProjectAdminComment.objects)
            _combine_objects(proj_ids, merged_proj.id, ProjectUserMessage.objects)
            _combine_objects(proj_ids, merged_proj.id, ProjectReview.objects)
            _combine_objects(proj_ids, merged_proj.id, ResearchOutput.objects)

            # Assign current user
            # assign_user(self.request.user, merged_proj)

            # Archive all old projects when done. Remove this for loop if you want to
            # keep them.
            for proj in projs_to_merge:
                archive_project(proj)

        return HttpResponseRedirect(reverse('project-list'))

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        
        proj_list = self.get_proj_list()
        formset = formset_factory(
            ProjectSelectForm, max_num=len(proj_list))
        formset = formset(initial=proj_list, prefix='projectform')
        context['formset'] = formset
        context['projects'] = Project.objects.all()
        context['rename'] = ProjectRenameForm
        return context

    def get_success_url(self):
        return reverse('project-detail', kwargs={'pk': self.object.pk})


class ProjectImportView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    model = Project
    template_name = 'project/project_import.html'

    def test_func(self) -> Optional[bool]:
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        if self.request.user.userprofile.is_pi:
            return True

    def post(self, request, *args, **kwargs):
        def _same_obj_exists(cur_obj, new_obj) -> bool:
            '''
            Pass through all fields of cur_obj to see if the deserialized object is
            the same object. If it is, return true, otherwise, return false.
            '''
            cur_vals = model_to_dict(cur_obj)
            new_vals = model_to_dict(new_obj)
            
            unique_keys = []

            for key in cur_obj._meta.fields:
                if key.unique and key.name != 'id':
                    unique_keys.append(key.name)

            for key in unique_keys:
                if cur_vals[key] == new_vals[key]:
                    return True

            # # id keys will be updated later in the code
            # for key in cur_vals.keys():
            #     cur_val = cur_vals[key]
            #     new_val = new_vals[key]

            #     if key != "id" and cur_val != new_val:
            #         return False
            
            return False

        form = ProjectImportForm(request.POST, request.FILES)

        if form.is_valid():
            file = request.FILES['file_upload']
            file_content = file.read()

            grouped_data = {}
            try:
                # The data at this point is grouped and each member needs to be
                # individually feed through the deserializer
                grouped_data = json.loads(file_content)
            except json.JSONDecodeError:
                # Error w/ json decoding
                messages.error(request, "JSON Decode Error: The project file you have selected is corrupted " + 
                    "or is in a format ColdFront does not recognise.")
                return HttpResponseRedirect(reverse('project-list'))
            except UnicodeDecodeError:
                # Thing uploaded in not in text form
                messages.error(request, "Unicode Decode Error: Please upload a JSON document.")
                return HttpResponseRedirect(reverse('project-list'))

            # Delete file_content. May or may not improve memory usage on large
            # projects.
            del file_content

            # Contains the translations between the imported pks and the
            # assigned ones.
            # Structure is as follows:
            # {
            #   "project.project" : { imported_pk:assigned_pk, imported_pk:assigned_pk, ... }
            #   ...
            # }
            pk_translation_table = {}

            # Deserialized object dictionary
            do_dict = {}

            # Step 1: Gather all deserialized objects into a dictionary
            for member in grouped_data:
                # data_obj has the complete data of the Django model
                data_obj = serializers.deserialize('json', member)

                name = re.search("(?<=\"model\": \").+?(?=\")", member)

                if name is not None:
                    do_dict[name[0]] = []

                    for obj in data_obj:
                        do_dict[name[0]].append(obj)

            # Contains list of models that will be ignored by _same_obj_exists
            duplicate_model_keys = {"project.projectuser", "allocation.allocationuser"}

            # Step 2: Assign unique pks. Build PK translation table at the same time.
            for key in do_dict:
                new_pk = 0

                pk_translation_table[key] = {}
                
                for obj in do_dict[key]:
                    abort_import: bool = False

                    # Pass through all objects of this type
                    for cur_obj in (type(obj.object)).objects.all():
                        # If key IS in duplicate_model_keys, then always import.
                        if key not in duplicate_model_keys and _same_obj_exists(cur_obj, obj.object):
                            abort_import = True
                            pk_translation_table[key][obj.object.pk] = cur_obj.pk
                            continue

                        if cur_obj.pk > new_pk:
                            # Find max PK
                            new_pk = cur_obj.pk

                    if abort_import:
                        continue
                    
                    new_pk += 1

                    pk_translation_table[key][obj.object.pk] = new_pk
                    obj.object.pk = new_pk
            
            # Translation table for Many to Many relationships of the same type of object.
            # Registration table for m2m.
            do_regist_tbl = {}

            # * NOTE: IF ANY FIELDS ARE LATER ADDED, THEY HAVE TO BE
            # * MANUALLY ADDED HERE IN ORDER FOR IT TO IMPORT CORRECTLY.
            # Step 3: Match fields. This has to be done manually.
            def _update_field(to_name: str, from_name: str, field_name: str, trans_tbl: dict):
                """
                Updates the ForeignKey fields of all deserialized objects whose model
                is to_name and whose field name is field_name.

                :param to_name: str: Name of deserialized object to update.
                :param from_name: str: Name of deserialized object to get PKs from.
                :param field_name: str: Name of the field to update.
                :param trans_tbl: dict: The pk translation table with old pks as keys and new
                pks as values.
                """
                if to_name not in do_dict:
                    # The requested field has not been placed in trans_tbl
                    # due to it not existing.
                    return
                
                for des_obj in do_dict[to_name]:
                    index = getattr(des_obj.object, field_name)
                    setattr(des_obj.object, field_name, trans_tbl[from_name][index])

            def _batch_update_field(to_names: Iterable, from_name: str, field_name: str, trans_tbl: dict):
                """
                Batch updates the ForeignKey fields of all deserialized objects whose model
                is in to_names and whose field name is field_name.

                :param to_names: Iterable[str]: Iterable of names of deserialized objects to update.
                :param from_name: str: Name of deserialized object to get PKs from.
                :param field_name: str: Name of the field to update.
                :param trans_tbl: dict: The pk translation table with old pks as keys and new
                pks as values.
                """
                for to_name in to_names:
                    _update_field(to_name, from_name, field_name, trans_tbl)

            def _update_m2m(to_name: str, from_name: str, m2m_name: str, trans_tbl: dict):
                """
                Updates the m2m fields of all the deserialized objects whose model
                is to_name and whose m2m name is m2m_name.

                :param to_name: str: Name of deserialized object to update.
                :param from_name: str: Name of deserialized object to get PKs from.
                :param m2m_name: str: Name of the m2m_data field to update.
                :param trans_tbl: dict: The pk translation table with old pks as keys and new
                pks as values.
                """
                if to_name not in do_dict or from_name not in trans_tbl:
                    # The requested m2m has not been placed in do_dict
                    # due to it not existing.
                    return
                
                for des_obj in do_dict[to_name]:
                    trans = [trans_tbl[from_name][ln] for ln in des_obj.m2m_data[m2m_name]]
                    des_obj.m2m_data[m2m_name] = trans
            
            def _register_m2m(to_name: str, from_name: str, m2m_name: str, regist_tbl: dict, trans_tbl: dict):
                """
                Updates the registration_table with all deserialized object whose model is
                to_name and whose m2m name is m2m_name. This should be only called if saving
                said model could cause an exception because of missing m2m members. Also removes
                said m2m fields from the deserialized object so that it can be saved without error.
                You will need to add the m2m after all objects of that type has been saved.

                :param to_name: str: Name of deserialized object to update.
                :param from_name: str: Name of deserialized object to get PKs from.
                :param m2m_name: str: Name of the m2m_data field to update.
                :param regist_tbl: dict: Dictionary to put all the m2m stuff.
                :param trans_tbl: dict: The pk translation table with old pks as keys and new
                pks as values.
                """
                if to_name not in do_dict:
                    # The requested m2m has not been placed in do_dict
                    # due to it not existing.
                    return

                if from_name not in regist_tbl:
                    regist_tbl[from_name] = {}

                if m2m_name not in regist_tbl[from_name]:
                    regist_tbl[from_name][m2m_name] = {}

                for des_obj in do_dict[to_name]:
                    trans = [trans_tbl[from_name][ln] for ln in des_obj.m2m_data[m2m_name]]
                    regist_tbl[from_name][m2m_name][des_obj.object.pk] = trans
                    des_obj.m2m_data[m2m_name] = []
            
            def _register_field(to_name: str, from_name: str, field_name: str, regist_tbl: dict, trans_tbl: dict):
                """
                Updates the registration_table with all deserialized object whose model is
                to_name and whose field name is field_name. This should be only called if saving
                said model could cause an exception because of missing field members. Also removes
                said field fields from the deserialized object so that it can be saved without error.
                You will need to add the field after all objects of that type has been saved.

                :param to_name: str: Name of deserialized object to update.
                :param from_name: str: Name of deserialized object to get PKs from.
                :param field_name: str: Name of the field_data field to update.
                :param regist_tbl: dict: Dictionary to put all the field stuff.
                :param trans_tbl: dict: The pk translation table with old pks as keys and new
                pks as values.
                """
                if to_name not in do_dict:
                    # The requested object has not been placed in do_dict
                    # due to it not existing.
                    return

                if from_name not in regist_tbl:
                    regist_tbl[from_name] = {}

                if field_name not in regist_tbl[from_name]:
                    regist_tbl[from_name][field_name] = {}

                for des_obj in do_dict[to_name]:
                    trans = getattr(des_obj.object, field_name)
                    regist_tbl[from_name][field_name][des_obj.object.pk] = trans
                    setattr(des_obj.object, field_name, None)

            def _reassign_m2m(name: str, query: QuerySet, m2m_name: str, 
                regist_tbl: dict, trans_tbl: dict):
                """
                Reassigns the m2m information to the object.

                :param name: str: Name of object to update.
                :param query: QuerySet: Query of the object to update.
                Usually object.objects.all().
                :param m2m_name: str: Name of the m2m_data field to update. Format:
                object.field
                :param regist_tbl: dict: Dictionary to get all the m2m stuff.
                """
                if name not in regist_tbl:
                    # The requested m2m has not been placed in regist_tbl
                    # due to it not existing.
                    return

                for obj in query:
                    if obj.pk not in trans_tbl[name][m2m_name]:
                        continue

                    for id in trans_tbl[name][m2m_name][obj.pk]:
                        getattr(obj, m2m_name).add(
                            query.filter(pk=id)[0]
                            )

            def _reassign_field(name: str, query: QuerySet, field_name: str, 
                regist_tbl: dict, trans_tbl: dict):
                """
                Reassigns the field information to the object.

                :param name: str: Name of object to update.
                :param query: QuerySet: Query of the object to update.
                Usually object.objects.all().
                :param field_name: str: Name of the field_data field to update. Format:
                object.field
                :param regist_tbl: dict: Dictionary to get all the field stuff.
                """
                if name not in regist_tbl:
                    # The requested field has not been placed in regist_tbl
                    # due to it not existing.
                    return

                for obj in query:
                    if obj.pk not in trans_tbl[name][field_name]:
                        continue

                    setattr(obj, field_name, trans_tbl[name][field_name][obj.pk])
                    obj.save(update_fields=[field_name])

            
            _update_field("project.project", "auth.user", "pi_id", pk_translation_table)
            _register_field("resource.resource", "resource.resource", "parent_resource_id", do_regist_tbl, pk_translation_table)
            _register_m2m("resource.resource", "resource.resource", "linked_resources", do_regist_tbl, pk_translation_table)
            _update_m2m("auth.user", "auth.group", "groups", pk_translation_table)
            _update_field("user.userprofile", "auth.user", "user_id", pk_translation_table)
            _update_m2m("resource.resource", "auth.user", "allowed_users", pk_translation_table)
            _update_m2m("resource.resource", "auth.group", "allowed_groups", pk_translation_table)
            _update_m2m("allocation.allocation", "resource.resource", "resources", pk_translation_table)
            _update_field("allocation.allocationuser", "auth.user", "user_id", pk_translation_table)
            _update_field("allocation.allocationuser", "allocation.allocation", "allocation_id", pk_translation_table)
            
            _batch_update_field(
                ["allocation.allocationchangerequest", "allocation.allocationattribute",
                "allocation.allocationadminnote", "allocation.allocationusernote"],
                "allocation.allocation", "allocation_id", pk_translation_table
            )
            _batch_update_field(
                ["allocation.allocationadminnote", "allocation.allocationusernote"],
                "auth.user", "author_id", pk_translation_table
            )

            # Add project ids
            # NOTE: If a model has a project ID, add it here to the list.
            _batch_update_field(
                [
                    "project.projectuser",
                    "allocation.allocation",
                    "publication.publication",
                    "grant.grant",
                    "project.projectadmincomment",
                    "project.projectusermessage",
                    "project.projectreview",
                    "research_output.researchoutput",
                ]
                , "project.project", "project_id", pk_translation_table
            )

            # Step 4: Save
            resave_proj = False

            for key in do_dict:
                for obj in do_dict[key]:
                    try:
                        obj.save()
                    except IntegrityError:
                        # Other object in many to many field not saved yet.
                        if isinstance(obj, Project):
                            resave_proj = True
                        pass

            _reassign_m2m("resource.resource", Resource.objects.all(),
                "linked_resources", do_dict, do_regist_tbl)
            _reassign_field("resource.resource", Resource.objects.all(),
                "parent_resource_id", do_dict, do_regist_tbl)

            if resave_proj:
                # Sometimes saving the project fails. Try again if that happens.
                do_dict['project.project'][0].object.save()
            
        return HttpResponseRedirect(reverse('project-list'))

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context['project_import_form'] = ProjectImportForm()
        return context

    def get_success_url(self):
        return reverse('project-detail', kwargs={'pk': self.object.pk})

def fix_serialize_data(data: QuerySet) -> list:
    """
    Wrapper function to serialize a QuerySet.

    :param data: QuerySet: QuerySet to serialize.
    """
    return serializers.serialize('json', data)

class ProjectExportView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'project/project_export.html'

    def test_func(self) -> Optional[bool]:
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))

        if project_obj.pi == self.request.user:
            return True

        if project_obj.projectuser_set.filter(user=self.request.user, role__name='Manager', status__name='Active').exists():
            return True

    def dispatch(self, request: http.HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        def _get_resource_query(proj_alloc):
            """
            Gets the resource query. Selects the resource(s) used by proj_alloc, and also selects
            any parent or linked resources.

            :param proj_alloc: The project allocation QuerySet
            :return: The resource QuerySet that contains all the resources required to export the
            project
            """
            resource_query = Resource.objects.filter(allocation__in=proj_alloc).distinct()
            old_resource_set = set()

            all_ids = set(resource_query.values_list('id', flat=True))

            # This while loop goes through all resources and follows all the links until all linked
            # resources are selected.
            # Note: Slow
            while old_resource_set != all_ids:
                old_resource_set = all_ids

                for resource in resource_query:
                    other_linked_ids = set(resource.linked_resources.all().values_list('id', flat=True))
                    all_ids = all_ids.union(other_linked_ids)
                    parent_resources = Resource.objects.filter(parent_resource__in=all_ids)
                    all_ids = all_ids.union(parent_resources.values_list('id', flat=True))
                    resource_query = Resource.objects.filter(pk__in=all_ids)
            
            return resource_query


        p_id = self.kwargs.get('pk')
        project_obj = get_object_or_404(Project, pk=p_id)
        if project_obj.status.name not in ['Active', 'New', ]:
            messages.error(
                request, 'You cannot export an archived project.')
            return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': p_id}))
        else:
            proj_users = ProjectUser.objects.filter(project_id=p_id)
            proj_alloc = Allocation.objects.filter(project_id=p_id)
            alloc_users = AllocationUser.objects.filter(allocation__in=proj_alloc)
            
            proj_resources = _get_resource_query(proj_alloc)
            # set(proj_resources.values_list("allowed_users", flat=True))

            resource_user_ids = set(proj_resources.values_list("allowed_users", flat=True))
            resource_group_ids = set(proj_resources.values_list("allowed_groups", flat=True))

            # Allocation attribute
            alloc_attrs = AllocationAttribute.objects.filter(allocation__in=proj_alloc)
            # Allocation change request
            alloc_change_request = AllocationChangeRequest.objects.filter(allocation__in=proj_alloc)

            # Allocation adim and user notes.
            # NOTE: Not sure if admin notes are actually being used. I just put it here in
            # case it is actually being used or if it will be in the future.
            alloc_admin_notes = AllocationAdminNote.objects.filter(allocation__in=proj_alloc)
            alloc_user_notes = AllocationUserNote.objects.filter(allocation__in=proj_alloc)

            # Set of all relevant users
            user_ids = set(itertools.chain(
                [p_user.user_id for p_user in proj_users],
                resource_user_ids,
                [note.author.id for note in alloc_admin_notes],
                [note.author.id for note in alloc_user_notes]
            ))
            

            serialized_data = [
                fix_serialize_data(Project.objects.filter(pk__exact=p_id)),    # Get current project
                fix_serialize_data(User.objects.filter(pk__in=user_ids)),
                fix_serialize_data(UserProfile.objects.filter(user_id__in=user_ids)),
                fix_serialize_data(Group.objects.filter(pk__in=resource_group_ids)),
                fix_serialize_data(proj_users),
                fix_serialize_data(Publication.objects.filter(project_id=p_id)),
                fix_serialize_data(Grant.objects.filter(project_id=p_id)),
                fix_serialize_data(proj_resources),
                fix_serialize_data(proj_alloc),
                fix_serialize_data(alloc_users),
                fix_serialize_data(alloc_attrs),
                fix_serialize_data(alloc_change_request),
                fix_serialize_data(alloc_admin_notes),
                fix_serialize_data(alloc_user_notes),
                fix_serialize_data(ProjectAdminComment.objects.filter(project_id=p_id)),
                fix_serialize_data(ProjectUserMessage.objects.filter(project_id=p_id)),
                fix_serialize_data(ProjectReview.objects.filter(project_id=p_id)),
                fix_serialize_data(ResearchOutput.objects.filter(project_id=p_id)),
            ]
            response = JsonResponse(serialized_data, content_type='application/json', safe=False, json_dumps_params={'indent': 2})
            response['Content-Disposition'] = 'attachment; filename="project.json"'
            
            return response

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context['project'] = Project.objects.get(pk=self.kwargs.get('pk'))
        return context


class ProjectAddUsersSearchView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'project/project_add_users.html'

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))

        if project_obj.pi == self.request.user:
            return True

        if project_obj.projectuser_set.filter(user=self.request.user, role__name='Manager', status__name='Active').exists():
            return True

    def dispatch(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))
        if project_obj.status.name not in ['Active', 'New', ]:
            messages.error(
                request, 'You cannot add users to an archived project.')
            return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': project_obj.pk}))
        else:
            return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context['user_search_form'] = UserSearchForm()
        context['project'] = Project.objects.get(pk=self.kwargs.get('pk'))
        return context


class ProjectAddUsersSearchResultsView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'project/add_user_search_results.html'
    raise_exception = True

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))

        if project_obj.pi == self.request.user:
            return True

        if project_obj.projectuser_set.filter(user=self.request.user, role__name='Manager', status__name='Active').exists():
            return True

    def dispatch(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))
        if project_obj.status.name not in ['Active', 'New', ]:
            messages.error(
                request, 'You cannot add users to an archived project.')
            return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': project_obj.pk}))
        else:
            return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        user_search_string = request.POST.get('q')
        search_by = request.POST.get('search_by')
        pk = self.kwargs.get('pk')

        project_obj = get_object_or_404(Project, pk=pk)

        users_to_exclude = [ele.user.username for ele in project_obj.projectuser_set.filter(
            status__name='Active')]

        cobmined_user_search_obj = CombinedUserSearch(
            user_search_string, search_by, users_to_exclude)

        context = cobmined_user_search_obj.search()

        matches = context.get('matches')
        for match in matches:
            match.update(
                {'role': ProjectUserRoleChoice.objects.get(name='User')})

        if matches:
            formset = formset_factory(ProjectAddUserForm, max_num=len(matches))
            formset = formset(initial=matches, prefix='userform')
            context['formset'] = formset
            context['user_search_string'] = user_search_string
            context['search_by'] = search_by

        if len(user_search_string.split()) > 1:
            users_already_in_project = []
            for ele in user_search_string.split():
                if ele in users_to_exclude:
                    users_already_in_project.append(ele)
            context['users_already_in_project'] = users_already_in_project

        # The following block of code is used to hide/show the allocation div in the form.
        if project_obj.allocation_set.filter(status__name__in=['Active', 'New', 'Renewal Requested']).exists():
            div_allocation_class = 'placeholder_div_class'
        else:
            div_allocation_class = 'd-none'
        context['div_allocation_class'] = div_allocation_class
        ###

        allocation_form = ProjectAddUsersToAllocationForm(
            request.user, project_obj.pk, prefix='allocationform')
        context['pk'] = pk
        context['allocation_form'] = allocation_form
        return render(request, self.template_name, context)


class ProjectAddUsersView(LoginRequiredMixin, UserPassesTestMixin, View):

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))

        if project_obj.pi == self.request.user:
            return True

        if project_obj.projectuser_set.filter(user=self.request.user, role__name='Manager', status__name='Active').exists():
            return True

    def dispatch(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))
        if project_obj.status.name not in ['Active', 'New', ]:
            messages.error(
                request, 'You cannot add users to an archived project.')
            return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': project_obj.pk}))
        else:
            return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        user_search_string = request.POST.get('q')
        search_by = request.POST.get('search_by')
        pk = self.kwargs.get('pk')

        project_obj = get_object_or_404(Project, pk=pk)

        users_to_exclude = [ele.user.username for ele in project_obj.projectuser_set.filter(
            status__name='Active')]

        cobmined_user_search_obj = CombinedUserSearch(
            user_search_string, search_by, users_to_exclude)

        context = cobmined_user_search_obj.search()

        matches = context.get('matches')
        for match in matches:
            match.update(
                {'role': ProjectUserRoleChoice.objects.get(name='User')})

        formset = formset_factory(ProjectAddUserForm, max_num=len(matches))
        formset = formset(request.POST, initial=matches, prefix='userform')

        allocation_form = ProjectAddUsersToAllocationForm(
            request.user, project_obj.pk, request.POST, prefix='allocationform')

        added_users_count = 0
        if formset.is_valid() and allocation_form.is_valid():
            project_user_active_status_choice = ProjectUserStatusChoice.objects.get(
                name='Active')
            allocation_user_active_status_choice = AllocationUserStatusChoice.objects.get(
                name='Active')
            allocation_form_data = allocation_form.cleaned_data['allocation']
            if '__select_all__' in allocation_form_data:
                allocation_form_data.remove('__select_all__')
            for form in formset:
                user_form_data = form.cleaned_data
                if user_form_data['selected']:
                    added_users_count += 1

                    # Will create local copy of user if not already present in local database
                    user_obj, _ = User.objects.get_or_create(
                        username=user_form_data.get('username'))
                    user_obj.first_name = user_form_data.get('first_name')
                    user_obj.last_name = user_form_data.get('last_name')
                    user_obj.email = user_form_data.get('email')
                    user_obj.save()

                    role_choice = user_form_data.get('role')
                    # Is the user already in the project?
                    if project_obj.projectuser_set.filter(user=user_obj).exists():
                        project_user_obj = project_obj.projectuser_set.get(
                            user=user_obj)
                        project_user_obj.role = role_choice
                        project_user_obj.status = project_user_active_status_choice
                        project_user_obj.save()
                    else:
                        project_user_obj = ProjectUser.objects.create(
                            user=user_obj, project=project_obj, role=role_choice, status=project_user_active_status_choice)

                    for allocation in Allocation.objects.filter(pk__in=allocation_form_data):
                        if allocation.allocationuser_set.filter(user=user_obj).exists():
                            allocation_user_obj = allocation.allocationuser_set.get(
                                user=user_obj)
                            allocation_user_obj.status = allocation_user_active_status_choice
                            allocation_user_obj.save()
                        else:
                            allocation_user_obj = AllocationUser.objects.create(
                                allocation=allocation,
                                user=user_obj,
                                status=allocation_user_active_status_choice)
                        allocation_activate_user.send(sender=self.__class__,
                                                      allocation_user_pk=allocation_user_obj.pk)

            messages.success(
                request, 'Added {} users to project.'.format(added_users_count))
        else:
            if not formset.is_valid():
                for error in formset.errors:
                    messages.error(request, error)

            if not allocation_form.is_valid():
                for error in allocation_form.errors:
                    messages.error(request, error)

        return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': pk}))


class ProjectRemoveUsersView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'project/project_remove_users.html'

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))

        if project_obj.pi == self.request.user:
            return True

        if project_obj.projectuser_set.filter(user=self.request.user, role__name='Manager', status__name='Active').exists():
            return True

    def dispatch(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))
        if project_obj.status.name not in ['Active', 'New', ]:
            messages.error(
                request, 'You cannot remove users from an archived project.')
            return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': project_obj.pk}))
        else:
            return super().dispatch(request, *args, **kwargs)

    def get_users_to_remove(self, project_obj):
        users_to_remove = [

            {'username': ele.user.username,
             'first_name': ele.user.first_name,
             'last_name': ele.user.last_name,
             'email': ele.user.email,
             'role': ele.role}

            for ele in project_obj.projectuser_set.filter(status__name='Active').order_by('user__username') if ele.user != self.request.user and ele.user != project_obj.pi
        ]

        return users_to_remove

    def get(self, request, *args, **kwargs):
        pk = self.kwargs.get('pk')
        project_obj = get_object_or_404(Project, pk=pk)

        users_to_remove = self.get_users_to_remove(project_obj)
        context = {}

        if users_to_remove:
            formset = formset_factory(
                ProjectRemoveUserForm, max_num=len(users_to_remove))
            formset = formset(initial=users_to_remove, prefix='userform')
            context['formset'] = formset

        context['project'] = get_object_or_404(Project, pk=pk)
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        pk = self.kwargs.get('pk')
        project_obj = get_object_or_404(Project, pk=pk)

        users_to_remove = self.get_users_to_remove(project_obj)

        formset = formset_factory(
            ProjectRemoveUserForm, max_num=len(users_to_remove))
        formset = formset(
            request.POST, initial=users_to_remove, prefix='userform')

        remove_users_count = 0

        if formset.is_valid():
            project_user_removed_status_choice = ProjectUserStatusChoice.objects.get(
                name='Removed')
            allocation_user_removed_status_choice = AllocationUserStatusChoice.objects.get(
                name='Removed')
            for form in formset:
                user_form_data = form.cleaned_data
                if user_form_data['selected']:

                    remove_users_count += 1

                    user_obj = User.objects.get(
                        username=user_form_data.get('username'))

                    if project_obj.pi == user_obj:
                        continue

                    project_user_obj = project_obj.projectuser_set.get(
                        user=user_obj)
                    project_user_obj.status = project_user_removed_status_choice
                    project_user_obj.save()

                    # get allocation to remove users from
                    allocations_to_remove_user_from = project_obj.allocation_set.filter(
                        status__name__in=['Active', 'New', 'Renewal Requested'])
                    for allocation in allocations_to_remove_user_from:
                        for allocation_user_obj in allocation.allocationuser_set.filter(user=user_obj, status__name__in=['Active', ]):
                            allocation_user_obj.status = allocation_user_removed_status_choice
                            allocation_user_obj.save()

                            allocation_remove_user.send(sender=self.__class__,
                                                        allocation_user_pk=allocation_user_obj.pk)

            messages.success(
                request, 'Removed {} users from project.'.format(remove_users_count))
        else:
            for error in formset.errors:
                messages.error(request, error)

        return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': pk}))


class ProjectUserDetail(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'project/project_user_detail.html'

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))

        if project_obj.pi == self.request.user:
            return True

        if project_obj.projectuser_set.filter(user=self.request.user, role__name='Manager', status__name='Active').exists():
            return True

    def get(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))
        project_user_pk = self.kwargs.get('project_user_pk')

        if project_obj.projectuser_set.filter(pk=project_user_pk).exists():
            project_user_obj = project_obj.projectuser_set.get(
                pk=project_user_pk)

            project_user_update_form = ProjectUserUpdateForm(
                initial={'role': project_user_obj.role, 'enable_notifications': project_user_obj.enable_notifications})

            context = {}
            context['project_obj'] = project_obj
            context['project_user_update_form'] = project_user_update_form
            context['project_user_obj'] = project_user_obj

            return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))
        project_user_pk = self.kwargs.get('project_user_pk')

        if project_obj.status.name not in ['Active', 'New', ]:
            messages.error(
                request, 'You cannot update a user in an archived project.')
            return HttpResponseRedirect(reverse('project-user-detail', kwargs={'pk': project_user_pk}))

        if project_obj.projectuser_set.filter(id=project_user_pk).exists():
            project_user_obj = project_obj.projectuser_set.get(
                pk=project_user_pk)

            if project_user_obj.user == project_user_obj.project.pi:
                messages.error(
                    request, 'PI role and email notification option cannot be changed.')
                return HttpResponseRedirect(reverse('project-user-detail', kwargs={'pk': project_user_pk}))

            project_user_update_form = ProjectUserUpdateForm(request.POST,
                                                             initial={'role': project_user_obj.role.name,
                                                                      'enable_notifications': project_user_obj.enable_notifications}
                                                             )

            if project_user_update_form.is_valid():
                form_data = project_user_update_form.cleaned_data
                project_user_obj.enable_notifications = form_data.get(
                    'enable_notifications')
                project_user_obj.role = ProjectUserRoleChoice.objects.get(
                    name=form_data.get('role'))
                project_user_obj.save()

                messages.success(request, 'User details updated.')
                return HttpResponseRedirect(reverse('project-user-detail', kwargs={'pk': project_obj.pk, 'project_user_pk': project_user_obj.pk}))


@login_required
def project_update_email_notification(request):

    if request.method == "POST":
        data = request.POST
        project_user_obj = get_object_or_404(
            ProjectUser, pk=data.get('user_project_id'))


        project_obj = project_user_obj.project

        allowed = False
        if project_obj.pi == request.user:
            allowed = True

        if project_obj.projectuser_set.filter(user=request.user, role__name='Manager', status__name='Active').exists():
            allowed = True

        if project_user_obj.user == request.user:
            allowed = True

        if request.user.is_superuser:
            allowed = True

        if allowed == False:
             return HttpResponse('not allowed', status=403)
        else:
            checked = data.get('checked')
            if checked == 'true':
                project_user_obj.enable_notifications = True
                project_user_obj.save()
                return HttpResponse('checked', status=200)
            elif checked == 'false':
                project_user_obj.enable_notifications = False
                project_user_obj.save()
                return HttpResponse('unchecked', status=200)
            else:
                return HttpResponse('no checked', status=400)
    else:
        return HttpResponse('no POST', status=400)


class ProjectReviewView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'project/project_review.html'
    login_url = "/"  # redirect URL if fail test_func

    def test_func(self):
        """ UserPassesTestMixin Tests"""
        if self.request.user.is_superuser:
            return True

        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))

        if project_obj.pi == self.request.user:
            return True

        if project_obj.projectuser_set.filter(user=self.request.user, role__name='Manager', status__name='Active').exists():
            return True

        messages.error(
            self.request, 'You do not have permissions to review this project.')

    def dispatch(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))

        if not project_obj.needs_review:
            messages.error(request, 'You do not need to review this project.')
            return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': project_obj.pk}))

        if 'Auto-Import Project'.lower() in project_obj.title.lower():
            messages.error(
                request, 'You must update the project title before reviewing your project. You cannot have "Auto-Import Project" in the title.')
            return HttpResponseRedirect(reverse('project-update', kwargs={'pk': project_obj.pk}))

        if 'We do not have information about your research. Please provide a detailed description of your work and update your field of science. Thank you!' in project_obj.description:
            messages.error(
                request, 'You must update the project description before reviewing your project.')
            return HttpResponseRedirect(reverse('project-update', kwargs={'pk': project_obj.pk}))

        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))
        project_review_form = ProjectReviewForm(project_obj.pk)

        context = {}
        context['project'] = project_obj
        context['project_review_form'] = project_review_form
        context['project_users'] = ', '.join(['{} {}'.format(ele.user.first_name, ele.user.last_name)
                                              for ele in project_obj.projectuser_set.filter(status__name='Active').order_by('user__last_name')])

        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        project_obj = get_object_or_404(Project, pk=self.kwargs.get('pk'))
        project_review_form = ProjectReviewForm(project_obj.pk, request.POST)

        project_review_status_choice = ProjectReviewStatusChoice.objects.get(
            name='Pending')

        if project_review_form.is_valid():
            form_data = project_review_form.cleaned_data
            project_review_obj = ProjectReview.objects.create(
                project=project_obj,
                reason_for_not_updating_project=form_data.get('reason'),
                status=project_review_status_choice)

            project_obj.force_review = False
            project_obj.save()

            domain_url = get_domain_url(self.request)
            url = '{}{}'.format(domain_url, reverse('project-review-list'))

            if EMAIL_ENABLED:
                send_email_template(
                    'New project review has been submitted',
                    'email/new_project_review.txt',
                    {'url': url},
                    EMAIL_SENDER,
                    [EMAIL_DIRECTOR_EMAIL_ADDRESS, ]
                )

            messages.success(request, 'Project reviewed successfully.')
            return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': project_obj.pk}))
        else:
            messages.error(
                request, 'There was an error in processing  your project review.')
            return HttpResponseRedirect(reverse('project-detail', kwargs={'pk': project_obj.pk}))


class ProjectReviewListView(LoginRequiredMixin, UserPassesTestMixin, ListView):

    model = ProjectReview
    template_name = 'project/project_review_list.html'
    prefetch_related = ['project', ]
    context_object_name = 'project_review_list'

    def get_queryset(self):
        return ProjectReview.objects.filter(status__name='Pending')

    def test_func(self):
        """ UserPassesTestMixin Tests"""

        if self.request.user.is_superuser:
            return True

        if self.request.user.has_perm('project.can_review_pending_project_reviews'):
            return True

        messages.error(
            self.request, 'You do not have permission to review pending project reviews.')


class ProjectReviewCompleteView(LoginRequiredMixin, UserPassesTestMixin, View):
    login_url = "/"

    def test_func(self):
        """ UserPassesTestMixin Tests"""

        if self.request.user.is_superuser:
            return True

        if self.request.user.has_perm('project.can_review_pending_project_reviews'):
            return True

        messages.error(
            self.request, 'You do not have permission to mark a pending project review as completed.')

    def get(self, request, project_review_pk):
        project_review_obj = get_object_or_404(
            ProjectReview, pk=project_review_pk)

        project_review_status_completed_obj = ProjectReviewStatusChoice.objects.get(
            name='Completed')
        project_review_obj.status = project_review_status_completed_obj
        project_review_obj.project.project_needs_review = False
        project_review_obj.save()

        messages.success(request, 'Project review for {} has been completed'.format(
            project_review_obj.project.title)
        )

        return HttpResponseRedirect(reverse('project-review-list'))


class ProjectReivewEmailView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    form_class = ProjectReviewEmailForm
    template_name = 'project/project_review_email.html'
    login_url = "/"

    def test_func(self):
        """ UserPassesTestMixin Tests"""

        if self.request.user.is_superuser:
            return True

        if self.request.user.has_perm('project.can_review_pending_project_reviews'):
            return True

        messages.error(
            self.request, 'You do not have permission to send email for a pending project review.')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pk = self.kwargs.get('pk')
        project_review_obj = get_object_or_404(ProjectReview, pk=pk)
        context['project_review'] = project_review_obj

        return context

    def get_form(self, form_class=None):
        """Return an instance of the form to be used in this view."""
        if form_class is None:
            form_class = self.get_form_class()
        return form_class(self.kwargs.get('pk'), **self.get_form_kwargs())

    def form_valid(self, form):
        pk = self.kwargs.get('pk')
        project_review_obj = get_object_or_404(ProjectReview, pk=pk)
        form_data = form.cleaned_data

        receiver_list = [project_review_obj.project.pi.email]
        cc = form_data.get('cc').strip()
        if cc:
            cc = cc.split(',')
        else:
            cc = []

        send_email(
            'Request for more information',
            form_data.get('email_body'),
            EMAIL_DIRECTOR_EMAIL_ADDRESS,
            receiver_list,
            cc
        )

        messages.success(self.request, 'Email sent to {} {} ({})'.format(
            project_review_obj.project.pi.first_name,
            project_review_obj.project.pi.last_name,
            project_review_obj.project.pi.username)
        )
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('project-review-list')
