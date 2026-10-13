"""
Test authenticated endpoints for bounty hunter dashboard.
Tests that protected endpoints return valid data with session_token cookie.
Creates a temporary test session and cleans up after.
"""
import pytest
import requests
import os
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
import secrets

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')
if not BASE_URL:
    BASE_URL = "https://execution-control-3.preview.emergentagent.com"


@pytest.fixture(scope="module")
def test_session():
    """Create a temporary test session for authenticated tests"""
    mongo_url = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
    db_name = os.environ.get('DB_NAME', 'test_database')
    client = MongoClient(mongo_url)
    db = client[db_name]
    
    # Get instance owner
    instance_config = db.instance_config.find_one({"_id": "instance_owner"})
    if not instance_config:
        pytest.skip("No instance owner configured")
    
    owner_user_id = instance_config.get("user_id")
    
    # Create a temporary test session (expires in 5 minutes)
    test_session_token = f"test_session_{secrets.token_hex(16)}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    
    session_doc = {
        "session_token": test_session_token,
        "user_id": owner_user_id,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at,
        "ip_address": "127.0.0.1",
        "user_agent": "TestAgent/1.0"
    }
    
    db.user_sessions.insert_one(session_doc)
    print(f"\nCreated test session for user {owner_user_id}")
    
    yield {"token": test_session_token, "user_id": owner_user_id}
    
    # Cleanup
    db.user_sessions.delete_one({"session_token": test_session_token})
    print(f"\nDeleted test session")
    client.close()


class TestAuthenticatedAccess:
    """Test that protected endpoints return valid data with session cookie"""
    
    def test_discovery_board_returns_data_with_auth(self, test_session):
        """Discovery board should return bounty data when authenticated"""
        response = requests.get(
            f"{BASE_URL}/api/bounty/discovery/board",
            cookies={"session_token": test_session["token"]}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Check required fields
        assert "items" in data, "Should have items array"
        assert "summary" in data, "Should have summary"
        assert "source_health" in data, "Should have source_health"
        
        # Check summary structure
        summary = data["summary"]
        assert "total" in summary, "Summary should have total"
        assert "runnable_now" in summary, "Summary should have runnable_now"
        
        print(f"Discovery board: {summary['total']} total items, {summary['runnable_now']} runnable now")
        print(f"Items returned: {len(data['items'])}")
    
    def test_execution_controls_returns_data_with_auth(self, test_session):
        """Execution controls should return control data when authenticated"""
        response = requests.get(
            f"{BASE_URL}/api/bounty/execution/controls",
            cookies={"session_token": test_session["token"]}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Check required fields
        assert "autonomous_execution" in data, "Should have autonomous_execution"
        assert "autonomous_escalation" in data, "Should have autonomous_escalation"
        assert "background_discovery" in data, "Should have background_discovery"
        assert "daily_budget_nano_usd" in data, "Should have daily_budget_nano_usd"
        assert "driver" in data, "Should have driver status"
        
        print(f"Execution controls: autonomous_execution={data['autonomous_execution']}, "
              f"background_discovery={data['background_discovery']}")
    
    def test_recommended_candidate_returns_data_with_auth(self, test_session):
        """Recommended candidate should return candidate data when authenticated"""
        response = requests.get(
            f"{BASE_URL}/api/bounty/execution/recommended-candidate",
            cookies={"session_token": test_session["token"]}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # May or may not have a recommended candidate
        if "recommended" in data and data["recommended"]:
            rec = data["recommended"]
            print(f"Recommended candidate: {rec.get('repository')}#{rec.get('issue_number')} - "
                  f"{rec.get('reward_amount')} {rec.get('reward_currency')}")
        else:
            print(f"No recommended candidate currently. Considered: {data.get('considered', 0)}, "
                  f"Live checked: {data.get('live_checked', 0)}")
    
    def test_review_queue_returns_data_with_auth(self, test_session):
        """Review queue should return queue data when authenticated"""
        response = requests.get(
            f"{BASE_URL}/api/bounty/execution/review-queue",
            cookies={"session_token": test_session["token"]}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Check required fields
        assert "buckets" in data, "Should have buckets"
        assert "counts" in data, "Should have counts"
        
        print(f"Review queue counts: {data['counts']}")
    
    def test_repos_returns_data_with_auth(self, test_session):
        """Repos endpoint should return repository data when authenticated"""
        response = requests.get(
            f"{BASE_URL}/api/bounty/repos",
            cookies={"session_token": test_session["token"]}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Check required fields
        assert "repositories" in data, "Should have repositories"
        assert "autonomous_budget" in data, "Should have autonomous_budget"
        
        print(f"Repositories: {len(data['repositories'])} repos")
        print(f"Autonomous budget: {data['autonomous_budget']}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
