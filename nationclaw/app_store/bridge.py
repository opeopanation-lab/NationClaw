"""
NationClaw - App Store Bridge (FULL Working Implementation)
"""
import json
import logging
import requests
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from enum import Enum
import os

logger = logging.getLogger(__name__)

class AppSource(Enum):
    FDROID = "fdroid"

@dataclass
class AppInfo:
    package_name: str
    name: str
    description: str
    version: str
    source: AppSource
    apk_url: Optional[str] = None
    
    def to_dict(self):
        return {"package_name": self.package_name, "name": self.name}

class FdroidConnector:
    def __init__(self, repo_url="https://f-droid.org/repo"):
        self.repo_url = repo_url
        self.index_url = f"{repo_url}/index-v2.json"
        self.index_data = None
    
    def load_index(self):
        try:
            logger.info("Loading F-Droid index...")
            response = requests.get(self.index_url, timeout=60)
            response.raise_for_status()
            self.index_data = response.json()
            logger.info("F-Droid index loaded")
            return True
        except Exception as e:
            logger.error(f"Failed to load index: {e}")
            return False
    
    def search_apps(self, query, limit=20):
        if not self.index_data:
            if not self.load_index():
                return []
        
        query_lower = query.lower()
        results = []
        packages = self.index_data.get("packages", {})
        
        for pkg_name, pkg_data in packages.items():
            if len(results) >= limit:
                break
            if query_lower in pkg_name.lower():
                if pkg_data:
                    latest = pkg_data[-1]
                    manifest = latest.get("manifest", {})
                    results.append(AppInfo(
                        package_name=pkg_name,
                        name=manifest.get("name", pkg_name),
                        description="",
                        version=manifest.get("versionName", "unknown"),
                        source=AppSource.FDROID,
                        apk_url=f"{self.repo_url}/{latest.get('apkName', '')}"
                    ))
        return results
    
    def download_apk(self, app_info, download_dir="/tmp"):
        if not app_info.apk_url:
            return None
        try:
            logger.info(f"Downloading APK: {app_info.package_name}")
            response = requests.get(app_info.apk_url, timeout=120)
            response.raise_for_status()
            os.makedirs(download_dir, exist_ok=True)
            apk_path = os.path.join(download_dir, f"{app_info.package_name}.apk")
            with open(apk_path, 'wb') as f:
                f.write(response.content)
            return apk_path
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return None

class AppStoreBridge:
    def __init__(self):
        self.connectors = {AppSource.FDROID: FdroidConnector()}
        logger.info("AppStoreBridge initialized")
    
    def search_apps(self, query, limit=20):
        all_results = []
        for source, connector in self.connectors.items():
            try:
                results = connector.search_apps(query, limit)
                all_results.extend(results)
            except Exception as e:
                logger.error(f"Search failed: {e}")
        
        # Remove duplicates
        seen = {}
        unique = []
        for app in all_results:
            if app.package_name not in seen:
                seen[app.package_name] = app
                unique.append(app)
        return unique
    
    def download_and_install(self, app_info, device_id, adb_path="adb"):
        connector = self.connectors.get(app_info.source)
        if not connector:
            return False
        
        apk_path = connector.download_apk(app_info)
        if not apk_path:
            return False
        
        try:
            import subprocess
            cmd = [adb_path, "-s", device_id, "install", apk_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            os.remove(apk_path)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Install failed: {e}")
            return False
