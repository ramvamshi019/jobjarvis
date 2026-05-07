from app.connectors.greenhouse import GreenhouseConnector
from app.connectors.lever import LeverConnector
from app.connectors.ashby import AshbyConnector
from app.connectors.smartrecruiters import SmartRecruitersConnector
from app.connectors.workday import WorkdayConnector
from app.connectors.icims import ICIMSConnector
from app.connectors.workable import WorkableConnector
from app.connectors.bamboohr import BambooHRConnector
from app.connectors.breezy import BreezyConnector
from app.connectors.recruitee import RecruiteeConnector
from app.connectors.custom_portal import CustomPortalConnector
from app.connectors.teamtailor import TeamTailorConnector

ATS_REGISTRY = {
    "greenhouse":    GreenhouseConnector,
    "lever":         LeverConnector,
    "ashby":         AshbyConnector,
    "smartrecruiters": SmartRecruitersConnector,
    "workday":       WorkdayConnector,
    "icims":         ICIMSConnector,
    "workable":      WorkableConnector,
    "bamboohr":      BambooHRConnector,
    "breezy":        BreezyConnector,
    "recruitee":     RecruiteeConnector,
    "custom_portal": CustomPortalConnector,
    "teamtailor":    TeamTailorConnector,
}

def get_connector(ats_type: str):
    return ATS_REGISTRY.get(ats_type.lower())
