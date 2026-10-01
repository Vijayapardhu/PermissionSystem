-- CSE Permission & Leave Tracking System Database Schema
-- Run this in MySQL to create the database and tables

CREATE DATABASE IF NOT EXISTS cse_permission_system CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE cse_permission_system;

-- Users table
-- Student mailboxes are provisioned as <rollno>@adityauniversity.in in lower
-- case (for example 26b21cs058@adityauniversity.in) and the Outlook display
-- name is the same roll number, so roll_number is the authoritative student
-- identifier. `name` mirrors it for students; staff keep their real name.
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    microsoft_id VARCHAR(255) UNIQUE,
    email VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    roll_number VARCHAR(50) UNIQUE,
    phone VARCHAR(20),
    role ENUM('STUDENT', 'LECTURER', 'HOD') NOT NULL DEFAULT 'STUDENT',
    department VARCHAR(100) DEFAULT 'CSE',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_email (email),
    INDEX idx_roll_number (roll_number),
    INDEX idx_role (role)
);

-- Permission requests table
CREATE TABLE IF NOT EXISTS permission_requests (
    id INT AUTO_INCREMENT PRIMARY KEY,
    student_id INT NOT NULL,
    permission_type ENUM('LEAVE', 'CLASSROOM') NOT NULL,
    reason TEXT NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    start_time TIME,
    end_time TIME,
    status ENUM('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED', 'EXPIRED') NOT NULL DEFAULT 'PENDING',
    assigned_faculty_id INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (assigned_faculty_id) REFERENCES users(id) ON DELETE SET NULL,
    INDEX idx_student_id (student_id),
    INDEX idx_status (status),
    INDEX idx_permission_type (permission_type),
    INDEX idx_dates (start_date, end_date)
);

-- Proof documents table
CREATE TABLE IF NOT EXISTS proof_documents (
    id INT AUTO_INCREMENT PRIMARY KEY,
    request_id INT NOT NULL,
    original_filename VARCHAR(255) NOT NULL,
    stored_filename VARCHAR(255) NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    file_type VARCHAR(50) NOT NULL,
    file_size INT NOT NULL,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (request_id) REFERENCES permission_requests(id) ON DELETE CASCADE,
    INDEX idx_request_id (request_id)
);

-- Approval history table
CREATE TABLE IF NOT EXISTS approval_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    request_id INT NOT NULL,
    faculty_id INT NOT NULL,
    action ENUM('APPROVED', 'REJECTED') NOT NULL,
    remarks TEXT,
    actioned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (request_id) REFERENCES permission_requests(id) ON DELETE CASCADE,
    FOREIGN KEY (faculty_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_request_id (request_id),
    INDEX idx_faculty_id (faculty_id)
);

-- Class groups -------------------------------------------------------------
-- A lecturer creates a class, then bulk-loads the roster from a spreadsheet of
-- roll numbers. Members can be stored before the student account exists
-- (student_id NULL), so an uploaded roster links up automatically once that
-- student signs in for the first time.

CREATE TABLE IF NOT EXISTS class_groups (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    section_code VARCHAR(40),
    academic_year VARCHAR(20),
    faculty_id INT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (faculty_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_faculty (faculty_id)
);

CREATE TABLE IF NOT EXISTS class_members (
    id INT AUTO_INCREMENT PRIMARY KEY,
    class_id INT NOT NULL,
    student_id INT,
    roll_number VARCHAR(50) NOT NULL,
    enrolled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (class_id) REFERENCES class_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE KEY uq_class_roll (class_id, roll_number),
    INDEX idx_class (class_id),
    INDEX idx_student (student_id),
    INDEX idx_roll_lookup (roll_number)
);

-- Attendance ---------------------------------------------------------------
-- One row per student per class per day. ON_PERMISSION marks a student excused
-- because an approved permission covers that date.

CREATE TABLE IF NOT EXISTS attendance_records (
    id INT AUTO_INCREMENT PRIMARY KEY,
    class_id INT NOT NULL,
    student_id INT NOT NULL,
    attendance_date DATE NOT NULL,
    status ENUM('PRESENT', 'ABSENT', 'ON_PERMISSION') NOT NULL,
    marked_by INT NOT NULL,
    marked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (class_id) REFERENCES class_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (marked_by) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE KEY uq_attendance (class_id, student_id, attendance_date),
    INDEX idx_class_date (class_id, attendance_date)
);

-- Staff accounts. microsoft_id is filled in automatically on first Entra sign-in,
-- so the placeholder below only reserves the email -> role mapping.
INSERT IGNORE INTO users (microsoft_id, email, name, role, department) VALUES 
('hod-microsoft-id', 'hod.cse@adityauniversity.in', 'Dr. S. Raghavan', 'HOD', 'CSE'),
('lecturer1-microsoft-id', 'lecturer1.cse@adityauniversity.in', 'Dr. Anil Kumar', 'LECTURER', 'CSE'),
('lecturer2-microsoft-id', 'lecturer2.cse@adityauniversity.in', 'Prof. Meera Sharma', 'LECTURER', 'CSE');

-- Students. The roll number doubles as the university mailbox prefix and as
-- the Outlook display name, which is why name mirrors it.
INSERT IGNORE INTO users (microsoft_id, email, name, roll_number, phone, role, department) VALUES 
('student1-microsoft-id', '26b21cs058@adityauniversity.in', '26B21CS058', '26B21CS058', '9876543210', 'STUDENT', 'CSE'),
('student2-microsoft-id', '26b21cs059@adityauniversity.in', '26B21CS059', '26B21CS059', '9876543211', 'STUDENT', 'CSE'),
('student3-microsoft-id', '25b21cs012@adityauniversity.in', '25B21CS012', '25B21CS012', '9876543212', 'STUDENT', 'CSE'),
('student4-microsoft-id', '24b21cs145@adityauniversity.in', '24B21CS145', '24B21CS145', '9876543213', 'STUDENT', 'CSE');