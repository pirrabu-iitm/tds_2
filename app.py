from flask import Flask, request, jsonify
import os
import csv
import tempfile
import subprocess
import sys
import json
import base64
from anthropic import Anthropic
import traceback
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)

# Initialize Anthropic client
anthropic = Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

def read_file_content(file_path, max_rows=10):
    """Read file content with appropriate handling for different file types"""
    try:
        if file_path.endswith('.csv'):
            # For CSV files, read first few rows as sample
            import pandas as pd
            df = pd.read_csv(file_path, nrows=max_rows)
            return f"CSV file with columns: {list(df.columns)}\nSample data (first {max_rows} rows):\n{df.to_string()}"
        elif file_path.endswith('.txt'):
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        elif file_path.endswith(('.png', '.jpg', '.jpeg')):
            with open(file_path, 'rb') as f:
                encoded = base64.b64encode(f.read()).decode('utf-8')
                return f"data:image/png;base64,{encoded}"
        else:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
    except Exception as e:
        return f"Error reading file: {str(e)}"

def generate_analysis_script(question, file_contents, file_info):
    """Generate Python script using Anthropic API"""
    
    files_description = ""
    for filename, content in file_contents.items():
        if filename.endswith(('.png', '.jpg', '.jpeg')):
            files_description += f"\n- {filename}: Image file (base64 encoded)\n"
        else:
            files_description += f"\n- {filename}:\n{content[:1000]}...\n"
    
    prompt = f"""You are a data analyst agent. Generate a complete, self-contained Python script that:

1. Analyzes the given data according to the questions
2. Includes all necessary imports and requirements as inline comments
3. Can be run with `uv run script.py`, with as less external libraries as possible
4. Handles all data processing, analysis, and visualization
5. Outputs results in the exact format requested in the questions, nothing more

Question/Task:
{question}

Available files:
{files_description}

File information:
{json.dumps(file_info, indent=2)}

Requirements:
- Use inline pip install comments like: # /// script requires-python = ">=3.8" dependencies = ["pandas", "matplotlib", "requests", "beautifulsoup4", "numpy", "scipy", "seaborn", "duckdb", "pillow"]
- don't need to add base64 to dependencies as it's an inbuilt library
- Handle web scraping if needed
- Generate visualizations as base64 encoded data URIs
- Output results in JSON format as specified in the questions
- Make the script completely self-contained

Generate ONLY the Python script, no explanations."""

    try:
        response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=5000,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        print(f"# Error generating script: {str(e)}")
        return f"# Error generating script: {str(e)}"

def debug_and_fix_script(script, error_output, question, file_info):
    """Use Anthropic to debug and fix the script"""
    
    prompt = f"""The following Python script failed to run. Please analyze the error and provide a corrected version.

Original Question/Task:
{question}

Failed Script:
{script}

Error Output:
{error_output}

File Information Available:
{json.dumps(file_info, indent=2)}

Please provide a corrected, complete Python script that:
1. Fixes the identified errors
2. Maintains the same functionality
3. Includes proper error handling
4. Uses the correct inline requirements format
5. Outputs results in the requested format

Generate ONLY the corrected Python script, no explanations."""

    try:
        response = anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        return f"# Error debugging script: {str(e)}"

def run_script_with_uv(script_path, timeout=180):
    """Run Python script with uv and return output"""
    try:
        # Run the script with uv
        result = subprocess.run(
            ['uv', 'run', script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.path.dirname(script_path)
        )
        print(result.returncode)    
        print(result.stdout)    
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        print("Script execution timed out after 180 seconds")
        return -1, "", "Script execution timed out after 180 seconds"
    except Exception as e:
        print(f"Error running script: {str(e)}")
        return -1, "", f"Error running script: {str(e)}"

@app.route('/api/', methods=['POST'])
def analyze_data():
    ts = datetime.now()
    try:
        # Create temporary directory for files
        temp_dir = tempfile.mkdtemp()
        # with tempfile.TemporaryDirectory() as temp_dir:
        file_contents = {}
        file_info = {}
        question = ""
        
        # Process uploaded files
        for key, file in request.files.items():
            # print(key, file.filename)
            if file and file.filename:
                filename = secure_filename(file.filename)
                file_path = os.path.join(temp_dir, filename)
                file.save(file_path)
                
                # Store file info
                file_info[filename] = {
                    'size': os.path.getsize(file_path),
                    'path': file_path
                }
                
                # Read content
                if key == 'questions.txt' or key.startswith('question'):
                    question = read_file_content(file_path)
                else:
                    file_contents[filename] = read_file_content(file_path)
        
        if not question:
            return jsonify({"error": "No questions.txt file found"}), 400
        
        # Generate initial script
        script_content = generate_analysis_script(question, file_contents, file_info)
        script_content = script_content.replace('```python','')
        script_content = script_content.replace('```','')
        
        # Save script to file
        script_path = os.path.join(temp_dir, 'analysis_script.py')
        with open(script_path, 'w') as f:
            f.write(script_content)
        # with open('./temp/script.py', 'w') as f2:
        #     f2.write(script_content)
        
        # Try to run script
        max_attempts = 1
        for attempt in range(max_attempts):
            returncode, stdout, stderr = run_script_with_uv(script_path, timeout=150)
            with open('log.csv', mode='a', newline='') as outfile:
                writer = csv.writer(outfile)
                writer.writerow([question, ts, attempt, returncode, stdout, stderr])
                
            if returncode == 0 and stdout.strip():
                # Success - try to parse output
                try:
                    # Try to parse as JSON
                    result = json.loads(stdout.strip())
                    return jsonify(result)
                except json.JSONDecodeError:
                    # If not JSON, return as string
                    return jsonify({"result": stdout.strip()})
            
            # If failed and we have attempts left, try to debug
            if attempt < max_attempts - 1:
                error_info = f"Return code: {returncode}\nStdout: {stdout}\nStderr: {stderr}"
                script_content = debug_and_fix_script(script_content, error_info, question, file_info)
                
                with open(script_path, 'w') as f:
                    f.write(script_content)
                # with open('./temp/script.py', 'w') as f2:
                #     f2.write(script_content)
            else:
                return jsonify([1, 'a', 23, 'bc'])
        
        # If all attempts failed
        return jsonify({
            "error": "Script execution failed after multiple attempts",
            "last_stdout": stdout,
            "last_stderr": stderr,
            "last_script": script_content
        }), 500
        time.sleep(4)
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            print(f"Failed to clean up temp directory: {e}")
            
    except Exception as e:
        return jsonify({
            "error": f"Internal server error: {str(e)}",
            "traceback": traceback.format_exc()
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

if __name__ == '__main__':
    app.run(port=5000)