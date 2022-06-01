from django import forms

from coldfront.core.publication.models import PublicationSource


class PublicationAddForm(forms.Form):
    title = forms.CharField(max_length=1024, required=True)
    author = forms.CharField(max_length=1024, required=True)
    year = forms.IntegerField(min_value=1500, max_value=2090, required=True)
    journal = forms.CharField(max_length=1024, required=True)
    source = forms.CharField(widget=forms.HiddenInput())  # initialized by view


class PublicationSearchForm(forms.Form):
    search_id = forms.CharField(
        label='Search ID or ORCID ID', widget=forms.Textarea, required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['search_id'].help_text = '<br/>Enter ID such as DOI or Bibliographic Code to search. Enter an ORCID ID to import works.'


class PublicationResultForm(forms.Form):
    title = forms.CharField(max_length=1024, disabled=True)
    author = forms.CharField(disabled=True)
    year = forms.CharField(max_length=4, disabled=True)
    journal = forms.CharField(max_length=1024, disabled=True)
    unique_id = forms.CharField(max_length=255, disabled=True)
    source_pk = forms.IntegerField(widget=forms.HiddenInput(), disabled=True)
    selected = forms.BooleanField(initial=False, required=False)


class PublicationUserSelectForm(forms.Form):
    username = forms.CharField(max_length=150, disabled=True)
    first_name = forms.CharField(max_length=30, required=False, disabled=True)
    last_name = forms.CharField(max_length=150, required=False, disabled=True)
    email = forms.EmailField(max_length=100, required=False, disabled=True)
    selected = forms.BooleanField(initial=False, required=False)


class PublicationDeleteForm(forms.Form):
    title = forms.CharField(max_length=255, disabled=True)
    year = forms.CharField(max_length=30, disabled=True)
    selected = forms.BooleanField(initial=False, required=False)


class PublicationExportForm(forms.Form):
    title = forms.CharField(max_length=255, disabled=True)
    year = forms.CharField(max_length=30, disabled=True)
    unique_id = forms.CharField(max_length=255, disabled=True)
    selected = forms.BooleanField(initial=False, required=False)
