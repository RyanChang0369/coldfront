import orcid #NEW REQUIREMENT: orcid (pip install orcid)

class OrcidAPI:
    '''
    Contains useful variables for ORCID
    functionalities
    '''
    # Constants for orc_api (ORCID API)
    # Change to match real OSIRIS application
    # Currently set to testing application (can only access sandbox info)
    INST_KEY = "APP-HLD5WRQGSNL1YEIM"
    INST_SECRET = "ecd2d7f8-4baf-427f-87b6-57a52c0e58df"

    # Default ColdFront webpage. Should match one of Redirect URIs
    # in ORCID dev tools.
    ORC_REDIRECT = "http://localhost:8000/"

    # String to regex ORCID id from any string
    ORC_RE_KEY = "(\d{4}-){3}\d{3}[0-9,X]"

    # Sets up orcid research info importing
    # Set sandbox to false on production
    # Requires institution key and institution secret
    # Currently set to testing application (can only access sandbox info)
    orc_api = orcid.PublicAPI(INST_KEY, INST_SECRET, sandbox=True)