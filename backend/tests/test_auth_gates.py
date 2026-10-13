"""
Test auth gates for bounty hunter dashboard.
Tests that protected endpoints return 401 without auth and bootstrap returns 200.
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')
if not BASE_URL:
    BASE_URL = "https://execution-control-3.preview.emergentagent.com"

class TestUnauthenticatedAccess:
    """Test that protected endpoints return 401 without session cookie"""
    
    def test_bootstrap_returns_200_without_auth(self):
        """Bootstrap endpoint should return 200 even without auth (preview mode)"""
        response = requests.get(f"{BASE_URL}/api/bounty/bootstrap")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        # Should have settings data
        assert "settings" in data, "Bootstrap should include settings"
        # Should indicate preview mode (no user)
        assert data.get("user") is None or data.get("preview") == True, "Should be in preview mode without auth"
        print(f"Bootstrap response keys: {list(data.keys())}")
    
    def test_discovery_board_returns_401_without_auth(self):
        """Discovery board endpoint should return 401 without session cookie"""
        response = requests.get(f"{BASE_URL}/api/bounty/discovery/board")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print(f"Discovery board 401 response: {response.json()}")
    
    def test_execution_controls_returns_401_without_auth(self):
        """Execution controls endpoint should return 401 without session cookie"""
        response = requests.get(f"{BASE_URL}/api/bounty/execution/controls")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print(f"Execution controls 401 response: {response.json()}")
    
    def test_recommended_candidate_returns_401_without_auth(self):
        """Recommended candidate endpoint should return 401 without session cookie"""
        response = requests.get(f"{BASE_URL}/api/bounty/execution/recommended-candidate")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print(f"Recommended candidate 401 response: {response.json()}")
    
    def test_review_queue_returns_401_without_auth(self):
        """Review queue endpoint should return 401 without session cookie"""
        response = requests.get(f"{BASE_URL}/api/bounty/execution/review-queue")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print(f"Review queue 401 response: {response.json()}")
    
    def test_repos_returns_401_without_auth(self):
        """Repos endpoint should return 401 without session cookie"""
        response = requests.get(f"{BASE_URL}/api/bounty/repos")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print(f"Repos 401 response: {response.json()}")


class TestBootstrapContent:
    """Test bootstrap endpoint returns expected data structure"""
    
    def test_bootstrap_has_required_fields(self):
        """Bootstrap should return all required fields for dashboard"""
        response = requests.get(f"{BASE_URL}/api/bounty/bootstrap")
        assert response.status_code == 200
        data = response.json()
        
        # Check required top-level fields
        required_fields = ["settings", "controls", "metrics", "finance"]
        for field in required_fields:
            assert field in data, f"Missing required field: {field}"
        
        # Check settings structure
        assert "mode" in data["settings"], "Settings should have mode"
        
        # Check controls structure
        assert isinstance(data["controls"], dict), "Controls should be a dict"
        
        # Check metrics structure
        assert isinstance(data["metrics"], dict), "Metrics should be a dict"
        
        # Check finance structure
        assert isinstance(data["finance"], dict), "Finance should be a dict"
        
        print(f"Bootstrap data structure validated. Mode: {data['settings'].get('mode')}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
